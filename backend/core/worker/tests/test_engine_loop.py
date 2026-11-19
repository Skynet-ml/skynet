"""Tests for the worker engine-loop host: boxes open only for steps (SKY-719).

The tests use a SQLite file, the local-filesystem object store, local
subprocess boxes and a fake model: no network and no paid call.
"""

from __future__ import annotations

import hashlib
import json
import shlex
import threading
from collections import Counter
from collections.abc import Iterator
from dataclasses import replace
from decimal import Decimal
from pathlib import Path
from typing import Any

import pytest
from sqlalchemy import create_engine, delete, select, update
from sqlalchemy.engine import Engine
from sqlalchemy.exc import OperationalError
from sqlalchemy.orm import Session

from core.billing.budgets import BudgetInsufficientError, BudgetService
from core.billing.operation_pricing import UnpricedOperationError
from core.billing.runtime import ProviderFailedError, UsagePendingError
from core.billing.usage_tags import LANE_KEY, current_tags, usage_scope
from core.models.blackbox import BlackboxStrategy
from core.sandbox.grants import METRIC_BOX_GRANTS, Grant, resolve_grants
from core.sandbox.trees import GrantDeniedError, MetricFailedError, TreeRejectedError
from core.service_gateway.optimization.blackbox import auto
from core.service_gateway.optimization.blackbox.harness_bridge import hidden_hashes
from core.service_gateway.optimization.blackbox.protocol import (
    TOO_DEEP_ARGUMENTS,
    EngineContext,
    EvalServer,
    FileTooLargeError,
    ModelReply,
    RepoSeed,
    Result,
    Task,
    ToolCall,
    ToolDefinition,
    tool_calls_from_openai,
)
from core.service_gateway.optimization.blackbox.registry import EngineCapabilities
from core.service_gateway.optimization.blackbox.sandbox import LocalSubprocessRuntime, SandboxSpec
from core.service_gateway.optimization.blackbox.sandbox_contract import SandboxSession
from core.service_gateway.optimization.blackbox.tree_sandbox import RuntimeBoxSource
from core.service_gateway.optimization.blackbox.upstream import AUTO_ENGINES
from core.storage import step_log_store
from core.storage.models import AgentStepModel, Base, BillingCustomerModel, JobModel
from core.storage.object_store import LocalObjectStore
from core.storage.step_log_store import SEQ_MAX, StepLogStore, step_id_for
from core.storage.tree_store import TreeEntry, TreeStore, object_id_of
from core.storage.workspace_store import WorkspaceStore
from core.worker import engine_loop
from core.worker.actors import LeaseExhaustedError, User
from core.worker.engine_loop import (
    ROOT_AGENT_ID,
    EngineLoop,
    LoopStoppedError,
    ModelCallBilledError,
    ModelCallFailedError,
    StepDivergedError,
)

RUN_ID = "run-1"
_SPEC = SandboxSpec(lifetime_seconds=60, allowed_hosts=(), operation_key="op")
_METRIC = "wc -c < outputs/answer.txt"
_FAKE_SECRET = "sk-fake-route-key-7f3a"


class _WorkerDiedError(BaseException):
    """The worker dies in the middle of a model call: like a kill, no handler of ``Exception`` sees it."""


@pytest.fixture
def engine(tmp_path: Path) -> Iterator[Engine]:
    """Yield a SQLite file engine with a funded wallet for ``alice`` and the running job ``run-1``.

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
                username="alice", stripe_customer_id="local-alice", balance_cents=10_000, grant_remaining=0
            )
        )
        session.add(JobModel(optimization_id=RUN_ID, username="alice", status="running", execution_generation=1))
        session.commit()
    yield database
    database.dispose()


@pytest.fixture
def step_log(engine: Engine, tmp_path: Path) -> Iterator[StepLogStore]:
    """Yield a step log with a short flush window, since each step waits until it is durable.

    Args:
        engine: The database.
        tmp_path: Pytest's temporary folder.

    Yields:
        The step log.
    """
    store = StepLogStore(engine, LocalObjectStore(tmp_path / "objects"), "worker-1", flush_window_seconds=0.02)
    yield store
    store.close()


@pytest.fixture
def trees(step_log: StepLogStore) -> TreeStore:
    """Return a tree store over the step log.

    Args:
        step_log: The step log.

    Returns:
        The tree store.
    """
    return TreeStore(step_log)


@pytest.fixture
def owner(engine: Engine) -> tuple[User, int]:
    """Return alice with a run budget on her workspace, and the budget's generation.

    Args:
        engine: The database.

    Returns:
        The user and the budget generation.
    """
    budgets = BudgetService(engine=engine)
    budget = budgets.create("alice", 1_000, idempotency_key="workspace-budget")
    budget = budgets.attach_to_job(budget.id, "alice", RUN_ID, expected_revision=budget.revision)
    alice = User("alice", engine=engine, budgets=budgets)
    WorkspaceStore(engine).set_budget(alice.workspace().id, budget.id)
    return alice, budget.generation


class _Boxes:
    """Count the boxes that the sources opened, and the ones that each agent has open now."""

    def __init__(self) -> None:
        """Start with no box."""
        self.lock = threading.Lock()
        self.open: Counter[str] = Counter()
        self.opened = 0


class _CountingSource(RuntimeBoxSource):
    """Open a local box for each acquire, and count it."""

    def __init__(self, boxes: _Boxes) -> None:
        """Bind the source to the shared counter.

        Args:
            boxes: The counter.
        """
        super().__init__(LocalSubprocessRuntime(), _SPEC)
        self._boxes = boxes
        self._agents: dict[int, str] = {}

    def acquire(self, agent_id: str, grants: Any) -> SandboxSession:
        """Open a box and count it.

        Args:
            agent_id: The agent that runs the step.
            grants: The step's grants.

        Returns:
            The box.
        """
        box = super().acquire(agent_id, grants)
        with self._boxes.lock:
            self._boxes.open[agent_id] += 1
            self._boxes.opened += 1
        self._agents[id(box)] = agent_id
        return box

    def release(self, box: SandboxSession, healthy: bool) -> None:
        """Close the box and count it as closed.

        Args:
            box: The box.
            healthy: Whether the step finished.
        """
        try:
            super().release(box, healthy)
        finally:
            with self._boxes.lock:
                self._boxes.open[self._agents.pop(id(box))] -= 1


class _BudgetSource(_CountingSource):
    """A box source whose budget refuses every box."""

    def acquire(self, agent_id: str, grants: Any) -> SandboxSession:
        """Refuse the box, as the budget does when it has no room.

        Args:
            agent_id: The agent that runs the step.
            grants: The step's grants.

        Raises:
            BudgetInsufficientError: Always.
        """
        with self._boxes.lock:
            self._boxes.opened += 1
        raise BudgetInsufficientError("The budget has no room for the box.")


class _Model:
    """A fake model: it appends ``x`` to the prompt's last word, and it records the caller's open boxes."""

    def __init__(self, boxes: _Boxes, *, die_at: int | None = None, fail_at: int | None = None) -> None:
        """Bind the model to the box counter.

        Args:
            boxes: The counter.
            die_at: The index of the call in which the worker dies; ``None`` never dies.
            fail_at: The index of the call that fails; ``None`` never fails.
        """
        self.boxes = boxes
        self.die_at = die_at
        self.fail_at = fail_at
        self.cost = Decimal(1)
        self.error: Exception | None = None
        self.reply_calls: list[ToolCall] | None = None
        self.prompts: list[str] = []
        self.tools: list[list[ToolDefinition] | None] = []
        self.open_boxes: list[int] = []

    def __call__(self, prompt: str | list[dict[str, Any]], tools: list[ToolDefinition] | None) -> ModelReply:
        """Answer one prompt, and return one tool call for each tool, or ``reply_calls`` when it is set.

        Args:
            prompt: The prompt text.
            tools: The tool definitions, or ``None``.

        Returns:
            The reply, its cost in cents and its tool calls.

        Raises:
            _WorkerDiedError: In the call at ``die_at``.
            ConnectionError: In the call at ``fail_at``.
            Exception: ``error``, in every call, when it is set.
        """
        self.open_boxes.append(self.boxes.open[current_tags().get(LANE_KEY) or ROOT_AGENT_ID])
        if len(self.prompts) == self.die_at:
            raise _WorkerDiedError
        if self.error is not None:
            raise self.error
        if len(self.prompts) == self.fail_at:
            self.fail_at = None
            raise ConnectionError(f"the provider dropped the call to https://llm.example/v1?key={_FAKE_SECRET}")
        self.prompts.append(str(prompt))
        self.tools.append(tools)
        text = f"{str(prompt).split()[-1]}x"
        calls: list[ToolCall] = [
            {"id": f"call_{index}", "name": tool["name"], "arguments": {"text": text}}
            for index, tool in enumerate(tools or [])
        ]
        return ModelReply(text, self.cost, calls if self.reply_calls is None else self.reply_calls)


class _FakeEngine:
    """A deterministic engine: it scores the seed, then asks the model for a better version each round."""

    name = "fake"

    def __init__(self, rounds: int) -> None:
        """Set the number of rounds.

        Args:
            rounds: How many model calls the engine makes.
        """
        self.rounds = rounds

    def run(self, task: Task, server: EvalServer, ctx: EngineContext) -> Result:
        """Improve the seed one round at a time.

        Args:
            task: The task with the seed.
            server: The eval server.
            ctx: The loop's context.

        Returns:
            The best version and its score.
        """
        candidate = best = str(task.seed_candidate)
        best_score = server.evaluate(candidate)[0] or 0.0
        for _ in range(self.rounds):
            candidate = ctx.reflection_lm(f"improve {candidate}")
            score = server.evaluate(candidate)[0] or 0.0
            if score > best_score:
                best, best_score = candidate, score
        return Result(best, best_score, server.used)


def _tree(trees: TreeStore, name: str, text: str) -> str:
    """Store one file as a tree.

    Args:
        trees: The tree store.
        name: The file name.
        text: The file text.

    Returns:
        The tree id.
    """
    return trees.put_tree(RUN_ID, [TreeEntry(name, "file", trees.put_blob(RUN_ID, text.encode()))])


def _store(trees: TreeStore, files: dict[str, bytes], links: dict[str, str] | None = None) -> str:
    """Store files and symlinks as a tree straight in the tree store, as the caller of a loop stores a seed.

    ``put_files`` refuses a hidden path, so a test that needs one in a tree writes it here.

    Args:
        trees: The tree store.
        files: The file bytes by ``/``-separated path.
        links: The symlink targets by ``/``-separated path.

    Returns:
        The tree id.
    """
    root: dict[str, Any] = {}
    for path, (mode, data) in [
        *((path, ("file", data)) for path, data in files.items()),
        *((path, ("symlink", target.encode())) for path, target in (links or {}).items()),
    ]:
        *folders, name = path.split("/")
        level = root
        for folder in folders:
            level = level.setdefault(folder, {})
        level[name] = (mode, data)

    def put(level: dict[str, Any]) -> str:
        """Store one folder and the folders below it.

        Args:
            level: The folder's entries by name: a nested folder, or a mode and its bytes.

        Returns:
            The folder's tree id.
        """
        return trees.put_tree(
            RUN_ID,
            [
                TreeEntry(name, "tree", put(value))
                if isinstance(value, dict)
                else TreeEntry(name, value[0], trees.put_blob(RUN_ID, value[1]))
                for name, value in level.items()
            ],
        )

    return put(root)


def _scorer(seed_tree: str, metric_tree: str) -> Any:
    """Return a step scorer: write the candidate in one box, and measure its length in a metric box.

    Args:
        seed_tree: The workspace tree that each command starts from.
        metric_tree: The metric's tree.

    Returns:
        The scorer.
    """

    def score(loop: EngineLoop, candidate: Any, case: Any) -> tuple[float | None, dict[str, Any]]:
        """Score one candidate.

        Args:
            loop: The engine loop.
            candidate: The candidate text.
            case: Not used.

        Returns:
            The candidate's length and no side information.
        """
        result, tree_id = loop.run(seed_tree, f"printf %s {shlex.quote(str(candidate))} > answer.txt")
        assert result.exit_code == 0
        return loop.score(tree_id, metric_tree, _METRIC), {}

    return score


def _loop(
    owner: tuple[User, int],
    engine: Engine,
    step_log: StepLogStore,
    trees: TreeStore,
    tmp_path: Path,
    *,
    generation: int,
    die_at: int | None = None,
    fail_at: int | None = None,
    budget_refuses: bool = False,
    model_max_cents: int | str = 2,
    hidden: dict[str, str] | None = None,
) -> tuple[EngineLoop, _Model, _Boxes]:
    """Start the run at a generation and build a loop over local boxes and the fake model.

    Args:
        owner: Alice and her budget generation.
        engine: The database.
        step_log: The step log.
        trees: The tree store.
        tmp_path: Pytest's temporary folder.
        generation: The job's execution generation.
        die_at: The model call in which the worker dies.
        fail_at: The model call that fails.
        budget_refuses: Whether the budget refuses every candidate box.
        model_max_cents: The bound of one model call; one lease is 10 cents.
        hidden: The run's hidden-path hashes.

    Returns:
        The loop, its model and its box counter.
    """
    user, budget_generation = owner
    run = user.workspace().start_run(
        RUN_ID,
        step_log=step_log,
        execution_generation=generation,
        budget_generation=budget_generation,
        grant_list_id="grants-1",
        chunk_cents=10,
        lease_slices=4,
    )
    boxes = _Boxes()
    model = _Model(boxes, die_at=die_at, fail_at=fail_at)
    loop = EngineLoop(
        run,
        engine=engine,
        trees=trees,
        candidates=(_BudgetSource if budget_refuses else _CountingSource)(boxes),
        metrics=_CountingSource(boxes),
        model=model,
        model_max_cents=model_max_cents,
        run_dir=tmp_path / f"loop-{generation}",
        hidden_hashes=hidden,
    )
    return loop, model, boxes


def _steps(engine: Engine, agent_id: str = ROOT_AGENT_ID) -> list[tuple[int, str, int]]:
    """Return the agent's committed steps.

    Args:
        engine: The database.
        agent_id: The agent.

    Returns:
        ``(seq, kind, execution_generation)`` for each step, in order.
    """
    with Session(engine) as session:
        rows = session.execute(
            select(AgentStepModel.seq, AgentStepModel.kind, AgentStepModel.execution_generation)
            .where(AgentStepModel.run_id == RUN_ID, AgentStepModel.agent_id == agent_id)
            .order_by(AgentStepModel.seq)
        )
        return [(row.seq, row.kind, row.execution_generation) for row in rows]


def _grant_list_ids(engine: Engine) -> list[str]:
    """Return the grant-list ids of the root agent's committed steps.

    Args:
        engine: The database.

    Returns:
        The ids, in step order.
    """
    with Session(engine) as session:
        return list(
            session.scalars(
                select(AgentStepModel.grant_list_id)
                .where(AgentStepModel.run_id == RUN_ID, AgentStepModel.agent_id == ROOT_AGENT_ID)
                .order_by(AgentStepModel.seq)
            )
        )


def _new_generation(engine: Engine) -> None:
    """Move the job to generation 2, as the orphan sweep does after a worker dies.

    Args:
        engine: The database.
    """
    with Session(engine) as session:
        session.execute(update(JobModel).where(JobModel.optimization_id == RUN_ID).values(execution_generation=2))
        session.commit()


_KINDS = ["command", "command", "model", "command", "command", "model", "command", "command"]


def test_a_model_call_has_no_box_open_and_each_call_commits_one_step(
    owner: tuple[User, int], engine: Engine, step_log: StepLogStore, trees: TreeStore, tmp_path: Path
) -> None:
    """Run the fake engine: one commit for each command and each model reply, and no box during a model call."""
    loop, model, boxes = _loop(owner, engine, step_log, trees, tmp_path, generation=1)
    scorer = _scorer(_tree(trees, "README", "seed"), _tree(trees, "metric.txt", "len"))

    result = _FakeEngine(rounds=2).run(Task("seed"), loop.server(scorer, max_evals=10), loop.context())

    assert (result.best_candidate, result.best_score) == ("seedxx", 6.0)
    assert model.prompts == ["improve seed", "improve seedx"]
    assert model.open_boxes == [0, 0]
    assert boxes.opened == 6
    assert not +boxes.open
    assert [kind for _, kind, _ in _steps(engine)] == _KINDS
    assert [seq for seq, _, _ in _steps(engine)] == list(range(len(_KINDS)))


def test_the_context_names_the_job_as_its_log_owner(
    owner: tuple[User, int], engine: Engine, step_log: StepLogStore, trees: TreeStore, tmp_path: Path
) -> None:
    """The context's log owner is the job id, so the worker's log router keeps the engine's forwarded records."""
    loop, _, _ = _loop(owner, engine, step_log, trees, tmp_path, generation=1)

    assert loop.context().log_owner == RUN_ID


def test_a_resumed_loop_continues_from_its_last_step_and_repeats_no_model_call(
    owner: tuple[User, int], engine: Engine, step_log: StepLogStore, trees: TreeStore, tmp_path: Path
) -> None:
    """Kill the worker in its second model call, then resume on a new generation."""
    seed_tree, metric_tree = _tree(trees, "README", "seed"), _tree(trees, "metric.txt", "len")
    first, first_model, _ = _loop(owner, engine, step_log, trees, tmp_path, generation=1, die_at=1)
    with pytest.raises(_WorkerDiedError):
        _FakeEngine(rounds=2).run(
            Task("seed"), first.server(_scorer(seed_tree, metric_tree), max_evals=10), first.context()
        )
    assert first_model.prompts == ["improve seed"]
    assert len(_steps(engine)) == 5

    _new_generation(engine)
    loop, model, boxes = _loop(owner, engine, step_log, trees, tmp_path, generation=2)
    result = _FakeEngine(rounds=2).run(
        Task("seed"), loop.server(_scorer(seed_tree, metric_tree), max_evals=10), loop.context()
    )

    assert (result.best_candidate, result.best_score) == ("seedxx", 6.0)
    # The committed reply comes back from the log; only the call that never committed runs again.
    assert model.prompts == ["improve seedx"]
    assert boxes.opened == 2
    assert not +boxes.open
    steps = _steps(engine)
    assert [kind for _, kind, _ in steps] == _KINDS
    assert [seq for seq, _, _ in steps] == list(range(len(_KINDS)))
    assert [generation for _, _, generation in steps] == [1] * 5 + [2] * 3
    # Session state lives only in the committed run trees, so neither the live run nor the resume writes a
    # local workspace that the two could disagree on.
    assert not (tmp_path / "loop-1" / "agents").exists()
    assert not (tmp_path / "loop-2" / "agents").exists()


def test_a_replay_that_asks_for_another_step_stops(
    owner: tuple[User, int], engine: Engine, step_log: StepLogStore, trees: TreeStore, tmp_path: Path
) -> None:
    """Resume with an engine that scores another first version: the eval server does not hide the divergence."""
    scorer = _scorer(_tree(trees, "README", "seed"), _tree(trees, "metric.txt", "len"))
    first, _, _ = _loop(owner, engine, step_log, trees, tmp_path, generation=1)
    _FakeEngine(rounds=1).run(Task("seed"), first.server(scorer, max_evals=10), first.context())

    _new_generation(engine)
    loop, _, boxes = _loop(owner, engine, step_log, trees, tmp_path, generation=2)
    with pytest.raises(StepDivergedError):
        loop.server(scorer, max_evals=10).evaluate("other")
    assert boxes.opened == 0


def test_a_fenced_loop_stops_before_any_more_model_or_box_work(
    owner: tuple[User, int], engine: Engine, step_log: StepLogStore, trees: TreeStore, tmp_path: Path
) -> None:
    """Move the run to a new generation under a live loop: no call after the move does model or box work.

    The engine catches the stop and calls again, as GEPA does around its reflection; the loop keeps the stop.
    """
    scorer = _scorer(_tree(trees, "README", "seed"), _tree(trees, "metric.txt", "len"))
    loop, model, boxes = _loop(owner, engine, step_log, trees, tmp_path, generation=1)
    assert loop.server(scorer, max_evals=10).evaluate("seed")[0] == 4.0
    assert boxes.opened == 2

    _new_generation(engine)
    with pytest.raises(LoopStoppedError):
        loop.server(scorer, max_evals=10).evaluate("seed")
    for _ in range(4):
        with pytest.raises(LoopStoppedError):
            loop.model("improve seed")
    with pytest.raises(LoopStoppedError):
        loop.server(scorer, max_evals=10).evaluate("seed")
    assert model.prompts == []
    assert boxes.opened == 2


def test_a_swallowed_divergence_keeps_the_loop_stopped(
    owner: tuple[User, int], engine: Engine, step_log: StepLogStore, trees: TreeStore, tmp_path: Path
) -> None:
    """An engine that catches a divergence and makes the committed call next gets no reply from the log.

    Each later call raises the kept stop's own type, so the engine still sees a divergence, not a plain stop.
    """
    first, _, _ = _loop(owner, engine, step_log, trees, tmp_path, generation=1)
    assert first.model("improve seed") == "seedx"

    _new_generation(engine)
    loop, model, _ = _loop(owner, engine, step_log, trees, tmp_path, generation=2)
    with pytest.raises(StepDivergedError) as diverged:
        loop.model("improve other")
    with pytest.raises(LoopStoppedError) as again:
        loop.model("improve seed")
    assert type(again.value) is StepDivergedError
    assert str(again.value) == str(diverged.value)
    assert again.value.__cause__ is diverged.value
    for later in (
        lambda: loop.put_files({"a": b"1"}),
        lambda: loop.list_files(_tree(trees, "a", "1")),
        loop.remaining_cost_usd,
        # A call that was already past the stop check meets the kept stop at the owner check.
        loop._check_owner,
    ):
        with pytest.raises(LoopStoppedError) as again:
            later()
        assert type(again.value) is StepDivergedError
    assert model.prompts == []


def test_a_kept_stop_that_is_no_divergence_stays_a_plain_stop(
    owner: tuple[User, int], engine: Engine, step_log: StepLogStore, trees: TreeStore, tmp_path: Path
) -> None:
    """A fenced loop keeps a plain stop, and a later call does not raise it as a divergence."""
    loop, model, _ = _loop(owner, engine, step_log, trees, tmp_path, generation=1)
    _new_generation(engine)
    with pytest.raises(LoopStoppedError) as first:
        loop.model("improve seed")
    with pytest.raises(LoopStoppedError) as again:
        loop.model("improve seed")
    assert type(first.value) is LoopStoppedError
    assert type(again.value) is LoopStoppedError
    assert model.prompts == []


def test_an_owner_check_database_error_in_a_scorer_stops_the_server_and_the_loop(
    owner: tuple[User, int],
    engine: Engine,
    step_log: StepLogStore,
    trees: TreeStore,
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """A dropped connection in the owner check is a kept stop, not a metric error that lets the run go on."""
    scorer = _scorer(_tree(trees, "README", "seed"), _tree(trees, "metric.txt", "len"))
    loop, _, boxes = _loop(owner, engine, step_log, trees, tmp_path, generation=1)

    def dropped(*args: Any) -> bool:
        """Fail as an owner query on a dropped database connection does.

        Args:
            *args: The owner check's arguments.

        Raises:
            OperationalError: Always.
        """
        raise OperationalError("owner", {}, Exception("connection dropped"))

    monkeypatch.setattr(engine_loop, "still_owns_run", dropped)
    server = loop.server(scorer, max_evals=10)
    with pytest.raises(LoopStoppedError, match="OperationalError"):
        server.evaluate("seed")
    with pytest.raises(LoopStoppedError):
        server.evaluate("seed")
    assert loop._stopped is not None
    assert boxes.opened == 0
    assert _steps(engine) == []


def test_an_agent_open_database_error_stops_the_loop_and_does_not_open_again(
    owner: tuple[User, int],
    engine: Engine,
    step_log: StepLogStore,
    trees: TreeStore,
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """A dropped connection while the agent opens is a kept stop: the next call opens nothing and calls no model."""
    loop, model, _ = _loop(owner, engine, step_log, trees, tmp_path, generation=1)
    reads: list[str] = []

    def dropped(*args: Any) -> dict[str, Any] | None:
        """Fail as a step-log read on a dropped database connection does.

        Args:
            *args: The read's arguments.

        Raises:
            OperationalError: Always.
        """
        reads.append(args[-1])
        raise OperationalError("last step", {}, Exception("connection dropped"))

    monkeypatch.setattr(engine_loop, "last_committed_step", dropped)
    with pytest.raises(LoopStoppedError, match="cannot open: OperationalError"):
        loop.model("improve seed")
    with pytest.raises(LoopStoppedError):
        loop.model("improve seed")
    assert reads == [ROOT_AGENT_ID]
    assert model.prompts == []


def test_a_budget_refusal_stops_the_loop(
    owner: tuple[User, int], engine: Engine, step_log: StepLogStore, trees: TreeStore, tmp_path: Path
) -> None:
    """A budget error from a box source is no metric error: the scorer stops the run and opens no more boxes."""
    scorer = _scorer(_tree(trees, "README", "seed"), _tree(trees, "metric.txt", "len"))
    loop, _, boxes = _loop(owner, engine, step_log, trees, tmp_path, generation=1, budget_refuses=True)
    server = loop.server(scorer, max_evals=10)

    with pytest.raises(LoopStoppedError, match="BudgetInsufficientError"):
        server.evaluate("seed")
    with pytest.raises(LoopStoppedError):
        server.evaluate("seed")
    assert boxes.opened == 1
    assert _steps(engine) == []


def test_a_model_cost_above_its_bound_stops_the_loop_after_one_paid_call(
    owner: tuple[User, int], engine: Engine, step_log: StepLogStore, trees: TreeStore, tmp_path: Path
) -> None:
    """A cost above the bound is billed and not committed, so the loop stops instead of billing again."""
    loop, model, _ = _loop(owner, engine, step_log, trees, tmp_path, generation=1)
    model.cost = Decimal(5)

    with pytest.raises(LoopStoppedError, match="outside its bound"):
        loop.model("improve seed")
    with pytest.raises(LoopStoppedError):
        loop.model("improve seed")
    assert model.prompts == ["improve seed"]
    assert _steps(engine) == []


def test_a_model_bound_above_one_lease_is_refused(
    owner: tuple[User, int], engine: Engine, step_log: StepLogStore, trees: TreeStore, tmp_path: Path
) -> None:
    """No lease could cover the bound, so the loop does not start."""
    with pytest.raises(ValueError, match="not between 0 and one lease"):
        _loop(owner, engine, step_log, trees, tmp_path, generation=1, model_max_cents=11)


def test_a_negative_model_bound_is_refused(
    owner: tuple[User, int], engine: Engine, step_log: StepLogStore, trees: TreeStore, tmp_path: Path
) -> None:
    """No model cost can be below 0, so the loop does not start."""
    with pytest.raises(ValueError, match="not between 0 and one lease"):
        _loop(owner, engine, step_log, trees, tmp_path, generation=1, model_max_cents=-1)


@pytest.mark.parametrize(
    "error",
    [
        UsagePendingError("The usage is not settled."),
        ProviderFailedError("The provider failed the response."),
        UnpricedOperationError("The operation has no price."),
        LeaseExhaustedError("No lease is left."),
        BudgetInsufficientError("The budget has no room."),
    ],
    ids=lambda error: type(error).__name__,
)
def test_a_stop_error_from_the_model_stops_the_loop_and_commits_no_step(
    error: Exception,
    owner: tuple[User, int],
    engine: Engine,
    step_log: StepLogStore,
    trees: TreeStore,
    tmp_path: Path,
) -> None:
    """A budget or lease error in a model call is no failed call: the loop stops, keeps the stop and commits nothing.

    Args:
        error: The stop error that the model raises.
    """
    loop, model, _ = _loop(owner, engine, step_log, trees, tmp_path, generation=1)
    model.error = error

    with pytest.raises(LoopStoppedError, match=type(error).__name__):
        loop.model("improve seed")
    model.error = None
    with pytest.raises(LoopStoppedError):
        loop.model("improve seed")
    assert model.prompts == []
    assert _steps(engine) == []


def test_a_gap_in_the_committed_log_stops_the_loop(
    owner: tuple[User, int], engine: Engine, step_log: StepLogStore, trees: TreeStore, tmp_path: Path
) -> None:
    """An agent whose log misses a step below its newest one cannot open, and the loop keeps the stop."""
    first, _, _ = _loop(owner, engine, step_log, trees, tmp_path, generation=1)
    assert first.model("improve seed") == "seedx"
    assert first.model("improve seedx") == "seedxx"
    with Session(engine) as session:
        session.execute(delete(AgentStepModel).where(AgentStepModel.run_id == RUN_ID, AgentStepModel.seq == 0))
        session.commit()

    _new_generation(engine)
    loop, model, _ = _loop(owner, engine, step_log, trees, tmp_path, generation=2)
    with pytest.raises(LoopStoppedError, match="cannot open: LookupError"):
        loop.model("improve seed")
    with pytest.raises(LoopStoppedError):
        loop.model("improve seed")
    assert model.prompts == []


def test_a_closed_run_stops_the_loop(
    owner: tuple[User, int], engine: Engine, step_log: StepLogStore, trees: TreeStore, tmp_path: Path
) -> None:
    """An agent cannot open in a closed run, and the loop keeps the stop."""
    loop, model, _ = _loop(owner, engine, step_log, trees, tmp_path, generation=1)
    loop._run.close()

    with pytest.raises(LoopStoppedError, match="cannot open: RuntimeError"):
        loop.model("improve seed")
    with pytest.raises(LoopStoppedError):
        loop.model("improve seed")
    assert model.prompts == []
    assert _steps(engine) == []


def test_a_model_bound_finer_than_the_ledger_is_refused(
    owner: tuple[User, int], engine: Engine, step_log: StepLogStore, trees: TreeStore, tmp_path: Path
) -> None:
    """The ledger holds nine fractional cent digits, so a finer bound would stop every model call."""
    with pytest.raises(ValueError, match="nine fractional digits"):
        _loop(owner, engine, step_log, trees, tmp_path, generation=1, model_max_cents="0.0000000001")


def test_a_run_closed_after_the_agent_opened_stops_the_model(
    owner: tuple[User, int], engine: Engine, step_log: StepLogStore, trees: TreeStore, tmp_path: Path
) -> None:
    """A run closed straight on the actor stops the next model step, and the loop keeps the stop."""
    loop, model, _ = _loop(owner, engine, step_log, trees, tmp_path, generation=1)
    assert loop.model("improve seed") == "seedx"
    loop._run.close()

    with pytest.raises(LoopStoppedError, match="is closed"):
        loop.model("improve seedx")
    with pytest.raises(LoopStoppedError):
        loop.model("improve seedx")
    assert model.prompts == ["improve seed"]
    assert len(_steps(engine)) == 1


@pytest.mark.parametrize("call", ["run", "score"])
def test_a_closed_loop_opens_no_box(
    call: str, owner: tuple[User, int], engine: Engine, step_log: StepLogStore, trees: TreeStore, tmp_path: Path
) -> None:
    """After the loop closes its run, a box step stops before any box opens and commits no step.

    Args:
        call: The box step: ``run`` or ``score``.
    """
    seed_tree, metric_tree = _tree(trees, "README", "seed"), _tree(trees, "metric.txt", "len")
    loop, _, boxes = _loop(owner, engine, step_log, trees, tmp_path, generation=1)
    assert loop.model("improve seed") == "seedx"
    loop.close()

    for _ in range(2):
        with pytest.raises(LoopStoppedError, match="is closed"):
            if call == "run":
                loop.run(seed_tree, "true")
            else:
                loop.score(seed_tree, metric_tree, _METRIC)
    assert boxes.opened == 0
    assert len(_steps(engine)) == 1


@pytest.mark.parametrize("call", ["run", "score"])
def test_a_run_closed_on_the_actor_opens_no_box(
    call: str, owner: tuple[User, int], engine: Engine, step_log: StepLogStore, trees: TreeStore, tmp_path: Path
) -> None:
    """A run closed straight on the actor stops a box step before any box opens, and the loop keeps the stop.

    Args:
        call: The box step: ``run`` or ``score``.
    """
    seed_tree, metric_tree = _tree(trees, "README", "seed"), _tree(trees, "metric.txt", "len")
    loop, _, boxes = _loop(owner, engine, step_log, trees, tmp_path, generation=1)
    assert loop.model("improve seed") == "seedx"
    loop._run.close()

    for _ in range(2):
        with pytest.raises(LoopStoppedError, match="is closed"):
            if call == "run":
                loop.run(seed_tree, "true")
            else:
                loop.score(seed_tree, metric_tree, _METRIC)
    assert boxes.opened == 0
    assert len(_steps(engine)) == 1


def test_an_agent_that_does_not_open_stops_the_loop(
    owner: tuple[User, int],
    engine: Engine,
    step_log: StepLogStore,
    trees: TreeStore,
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """An open that fails would fail on every call, so the agent cannot open and the loop keeps the stop."""
    first, _, _ = _loop(owner, engine, step_log, trees, tmp_path, generation=1)
    first.run(_tree(trees, "README", "seed"), "printf a > answer.txt")
    reads: list[Any] = []

    def broken(*args: Any) -> None:
        """Fail as a step log read with a bad row does.

        Args:
            *args: The read's arguments.

        Raises:
            ValueError: Always.
        """
        reads.append(args)
        raise ValueError("a row is bad")

    monkeypatch.setattr(engine_loop, "last_committed_step", broken)
    _new_generation(engine)
    loop, model, _ = _loop(owner, engine, step_log, trees, tmp_path, generation=2)
    with pytest.raises(LoopStoppedError, match="cannot open: ValueError"):
        loop.model("improve seed")
    with pytest.raises(LoopStoppedError):
        loop.model("improve seed")
    assert len(reads) == 1
    assert model.prompts == []


def test_a_slow_agent_open_does_not_block_a_stop(
    owner: tuple[User, int],
    engine: Engine,
    step_log: StepLogStore,
    trees: TreeStore,
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """An agent's open holds only its own lock, so the loop closes while the agent still reads its log."""
    first, _, _ = _loop(owner, engine, step_log, trees, tmp_path, generation=1)
    first.run(_tree(trees, "README", "seed"), "printf a > answer.txt")
    entered, release = threading.Event(), threading.Event()
    read = engine_loop.last_committed_step

    def slow(*args: Any) -> Any:
        """Read the agent's last step slowly: wait until the test lets the read end.

        Args:
            *args: The read's arguments.

        Returns:
            What the real read returns.
        """
        entered.set()
        release.wait(10)
        return read(*args)

    monkeypatch.setattr(engine_loop, "last_committed_step", slow)
    _new_generation(engine)
    loop, model, _ = _loop(owner, engine, step_log, trees, tmp_path, generation=2)
    errors: list[BaseException] = []

    def call() -> None:
        """Make the first model call, which opens the agent."""
        try:
            loop.model("improve seed")
        except BaseException as error:
            errors.append(error)

    caller = threading.Thread(target=call)
    caller.start()
    assert entered.wait(10)
    closer = threading.Thread(target=loop.close)
    closer.start()
    closer.join(5)
    stopped_during_open = not closer.is_alive()
    release.set()
    caller.join(10)
    closer.join(10)

    assert stopped_during_open
    assert len(errors) == 1
    assert isinstance(errors[0], LoopStoppedError)
    assert model.prompts == []


def test_a_failed_model_call_commits_and_its_replay_fails_again_with_no_call(
    owner: tuple[User, int], engine: Engine, step_log: StepLogStore, trees: TreeStore, tmp_path: Path
) -> None:
    """A failed model call is one committed step with no cost, so the resume does not diverge after it."""
    first, first_model, _ = _loop(owner, engine, step_log, trees, tmp_path, generation=1, fail_at=0)
    with pytest.raises(ModelCallFailedError, match="ConnectionError"):
        first.model("improve seed")
    assert first.model("improve other") == "otherx"
    assert first_model.prompts == ["improve other"]
    failed = step_log.get_step(step_id_for(RUN_ID, ROOT_AGENT_ID, 0))
    assert Decimal(failed["body"]["cost_cents"]) == 0
    # The provider message holds a secret, so the step log keeps only the exception class.
    assert failed["body"]["error"] == "ConnectionError"
    assert _FAKE_SECRET not in json.dumps(failed)
    assert [kind for _, kind, _ in _steps(engine)] == ["model", "model"]

    _new_generation(engine)
    loop, model, _ = _loop(owner, engine, step_log, trees, tmp_path, generation=2)
    with pytest.raises(ModelCallFailedError, match="ConnectionError"):
        loop.model("improve seed")
    assert loop.model("improve other") == "otherx"
    assert loop.model("improve otherx") == "otherxx"
    assert model.prompts == ["improve otherx"]


def test_a_failure_record_names_the_grant_list_that_the_step_resolved(
    owner: tuple[User, int], engine: Engine, step_log: StepLogStore, trees: TreeStore, tmp_path: Path
) -> None:
    """A failed metric records the metric box's grant list; a denied grant list records the run's."""
    outputs, metric = _tree(trees, "answer.txt", "a"), _tree(trees, "metric.txt", "len")
    loop, _, _ = _loop(owner, engine, step_log, trees, tmp_path, generation=1)
    with pytest.raises(MetricFailedError):
        loop.score(outputs, metric, "exit 3")
    loop.score(outputs, metric, _METRIC)
    with pytest.raises(GrantDeniedError):
        loop.run(outputs, "true", (Grant("api.example.com", credential=True),))

    metric_grants = resolve_grants(METRIC_BOX_GRANTS, {}).grant_list_id
    assert _grant_list_ids(engine) == [metric_grants, metric_grants, "grants-1"]


def test_a_replayed_run_step_with_other_grants_diverges(
    owner: tuple[User, int], engine: Engine, step_log: StepLogStore, trees: TreeStore, tmp_path: Path
) -> None:
    """The grant list is part of a run step's fingerprint, so the same command with other grants is no match."""
    seed = _tree(trees, "README", "seed")
    first, _, _ = _loop(owner, engine, step_log, trees, tmp_path, generation=1)
    first.run(seed, "true")

    _new_generation(engine)
    loop, _, boxes = _loop(owner, engine, step_log, trees, tmp_path, generation=2)
    with pytest.raises(StepDivergedError):
        loop.run(seed, "true", (Grant("example.com"),))
    assert boxes.opened == 0


def test_a_failed_box_step_commits_and_its_replay_fails_again_with_no_box(
    owner: tuple[User, int], engine: Engine, step_log: StepLogStore, trees: TreeStore, tmp_path: Path
) -> None:
    """A metric with no score is one committed step, and the replay raises it again."""
    outputs, metric = _tree(trees, "answer.txt", "a"), _tree(trees, "metric.txt", "len")
    first, _, _ = _loop(owner, engine, step_log, trees, tmp_path, generation=1)
    with pytest.raises(MetricFailedError) as live_error:
        first.score(outputs, metric, "exit 3")
    assert first.score(outputs, metric, _METRIC) == 1.0
    assert [kind for _, kind, _ in _steps(engine)] == ["command", "command"]

    _new_generation(engine)
    loop, _, boxes = _loop(owner, engine, step_log, trees, tmp_path, generation=2)
    with pytest.raises(MetricFailedError) as replayed_error:
        loop.score(outputs, metric, "exit 3")
    assert str(live_error.value) == str(replayed_error.value)
    assert loop.score(outputs, metric, _METRIC) == 1.0
    assert boxes.opened == 0


def test_a_resume_that_scores_other_outputs_diverges_with_no_box(
    owner: tuple[User, int], engine: Engine, step_log: StepLogStore, trees: TreeStore, tmp_path: Path
) -> None:
    """A resume that scores another outputs tree with the same metric command gets no committed score."""
    metric = _tree(trees, "metric.txt", "len")
    first, _, _ = _loop(owner, engine, step_log, trees, tmp_path, generation=1)
    first.score(_tree(trees, "answer.txt", "abc"), metric, _METRIC)

    _new_generation(engine)
    resumed, _, boxes = _loop(owner, engine, step_log, trees, tmp_path, generation=2)
    with pytest.raises(StepDivergedError):
        resumed.score(_tree(trees, "answer.txt", "other"), metric, _METRIC)
    assert boxes.opened == 0


class _LaneEngine:
    """An Auto lane fixture: one model call, then every evaluation that the lane may make."""

    def __init__(self, name: str) -> None:
        """Set the engine id.

        Args:
            name: The engine id.
        """
        self.name = name

    def run(self, task: Task, server: EvalServer, ctx: EngineContext) -> Result:
        """Ask the model once, then score the reply on the lane's whole allowance.

        Args:
            task: The task with the lane's seed.
            server: The lane's eval server.
            ctx: The lane's context.

        Returns:
            The reply and its mean score.
        """
        candidate = ctx.reflection_lm(f"{self.name} {task.seed_candidate}")
        scores = [server.evaluate(candidate, {"i": i})[0] or 0.0 for i in range(server.remaining)]
        return Result(candidate, sum(scores) / len(scores), len(scores))


def test_auto_composes_its_lanes_on_the_loop_with_one_agent_for_each_lane(
    owner: tuple[User, int],
    engine: Engine,
    step_log: StepLogStore,
    trees: TreeStore,
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Run Auto on the loop: each lane and the continuation commit to their own agent."""
    monkeypatch.setattr(auto, "get_engine", lambda name, caps: _LaneEngine(name))
    loop, model, boxes = _loop(owner, engine, step_log, trees, tmp_path, generation=1)
    scorer = _scorer(_tree(trees, "README", "seed"), _tree(trees, "metric.txt", "len"))
    total_evals = 2 * (len(AUTO_ENGINES) + 1)

    result, lanes = auto.run_strategy(
        BlackboxStrategy(),
        Task("seed"),
        loop.server(scorer, max_evals=total_evals),
        loop.context(),
        caps=EngineCapabilities(proposer_available=True),
    )

    assert result.best_candidate is not None
    assert [lane.status for lane in lanes] == ["completed"] * len(lanes)
    assert model.open_boxes == [0] * len(model.prompts)
    assert not +boxes.open
    lanes_run = [auto.lane_tag("explore", name) for name in AUTO_ENGINES] + [auto.lane_tag("continue", "gepa")]
    lane_agents = [f"{ROOT_AGENT_ID}/{lane}" for lane in lanes_run]
    for agent_id in lane_agents:
        steps = _steps(engine, agent_id)
        # The first command step is the lane's committed budget room read.
        assert [kind for _, kind, _ in steps] == ["command", "model", "command", "command", "command", "command"], (
            agent_id
        )
        assert [seq for seq, _, _ in steps] == list(range(6))
    assert len(model.prompts) == len(lane_agents)


def test_a_fenced_lane_stops_the_auto_run(
    owner: tuple[User, int],
    engine: Engine,
    step_log: StepLogStore,
    trees: TreeStore,
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """A lane whose run lost the fence does not fail alone: the Auto run stops with no model call."""
    monkeypatch.setattr(auto, "get_engine", lambda name, caps: _LaneEngine(name))
    loop, model, boxes = _loop(owner, engine, step_log, trees, tmp_path, generation=1)
    scorer = _scorer(_tree(trees, "README", "seed"), _tree(trees, "metric.txt", "len"))
    _new_generation(engine)

    with pytest.raises(LoopStoppedError):
        auto.run_strategy(
            BlackboxStrategy(),
            Task("seed"),
            loop.server(scorer, max_evals=2 * (len(AUTO_ENGINES) + 1)),
            loop.context(),
            caps=EngineCapabilities(proposer_available=True),
        )
    assert model.prompts == []
    assert boxes.opened == 0


class _ScoringLaneEngine:
    """An Auto lane fixture that only scores the seed, with no model call."""

    def __init__(self, name: str) -> None:
        """Set the engine id.

        Args:
            name: The engine id.
        """
        self.name = name

    def run(self, task: Task, server: EvalServer, ctx: EngineContext) -> Result:
        """Score the seed on the lane's whole allowance.

        Args:
            task: The task with the lane's seed.
            server: The lane's eval server.
            ctx: The lane's context; not used.

        Returns:
            The seed and its mean score.
        """
        scores = [server.evaluate(task.seed_candidate, {"i": i})[0] or 0.0 for i in range(server.remaining)]
        return Result(task.seed_candidate, sum(scores) / len(scores), len(scores))


def test_a_fenced_lane_scorer_stops_the_auto_run(
    owner: tuple[User, int],
    engine: Engine,
    step_log: StepLogStore,
    trees: TreeStore,
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """A lane's scorer stops through the eval server after the fence moves, and the Auto run stops with no box."""
    monkeypatch.setattr(auto, "get_engine", lambda name, caps: _ScoringLaneEngine(name))
    loop, _, boxes = _loop(owner, engine, step_log, trees, tmp_path, generation=1)
    scorer = _scorer(_tree(trees, "README", "seed"), _tree(trees, "metric.txt", "len"))
    _new_generation(engine)

    with pytest.raises(LoopStoppedError):
        auto.run_strategy(
            BlackboxStrategy(),
            Task("seed"),
            loop.server(scorer, max_evals=2 * (len(AUTO_ENGINES) + 1)),
            loop.context(),
            caps=EngineCapabilities(proposer_available=True),
        )
    assert boxes.opened == 0


def test_a_settle_failure_after_the_commit_stops_the_loop_and_the_resume_replays_the_reply(
    owner: tuple[User, int],
    engine: Engine,
    step_log: StepLogStore,
    trees: TreeStore,
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """The step commits before the settle fails, so the live engine gets no result, and the resume agrees."""
    budgets = owner[0].budgets
    settle = budgets.settle
    failed: list[bool] = []

    def flaky(*args: Any, **kwargs: Any) -> Any:
        """Fail the first step settle, as a dropped database connection does.

        Args:
            *args: The settle's arguments.
            **kwargs: The settle's keyword arguments.

        Returns:
            What the real settle returns.

        Raises:
            OperationalError: In the first step settle.
        """
        if not kwargs.get("final") and not failed:
            failed.append(True)
            raise OperationalError("settle", {}, Exception("connection dropped"))
        return settle(*args, **kwargs)

    monkeypatch.setattr(budgets, "settle", flaky)
    loop, model, _ = _loop(owner, engine, step_log, trees, tmp_path, generation=1)
    with pytest.raises(LoopStoppedError, match="OperationalError"):
        loop.model("improve a")
    with pytest.raises(LoopStoppedError):
        loop.model("improve b")
    assert model.prompts == ["improve a"]
    step_log.durable(step_id_for(RUN_ID, ROOT_AGENT_ID, 0), 1)
    assert [kind for _, kind, _ in _steps(engine)] == ["model"]

    _new_generation(engine)
    resumed, resumed_model, _ = _loop(owner, engine, step_log, trees, tmp_path, generation=2)
    assert resumed.model("improve a") == "ax"
    assert resumed.model("improve ax") == "axx"
    assert resumed_model.prompts == ["improve ax"]


def test_a_closed_step_log_stops_the_box_steps_after_one_box(
    owner: tuple[User, int], engine: Engine, step_log: StepLogStore, trees: TreeStore, tmp_path: Path
) -> None:
    """A step log closed alone fails every commit; it has no public closed state, so one box opens, then the loop
    stops."""
    seed_tree = _tree(trees, "answer.txt", "a")
    loop, _, boxes = _loop(owner, engine, step_log, trees, tmp_path, generation=1)
    assert loop.model("improve a") == "ax"
    step_log.close()

    with pytest.raises(LoopStoppedError, match="the step log is closed"):
        loop.run(seed_tree, "true")
    for _ in range(2):
        with pytest.raises(LoopStoppedError):
            loop.run(seed_tree, "true")
    assert boxes.opened == 1


def test_a_durable_failure_after_a_box_step_commits_stops_the_loop(
    owner: tuple[User, int],
    engine: Engine,
    step_log: StepLogStore,
    trees: TreeStore,
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """The step keeps its seq when its durable wait fails, and the loop stops, so the live engine gets no result."""
    seed_tree = _tree(trees, "README", "seed")
    loop, _, boxes = _loop(owner, engine, step_log, trees, tmp_path, generation=1)

    def lost(*args: Any, **kwargs: Any) -> None:
        """Fail the durable wait, as a wait that times out does.

        Args:
            *args: The wait's arguments.
            **kwargs: The wait's keyword arguments.

        Raises:
            TimeoutError: Always.
        """
        raise TimeoutError("the step did not become durable")

    monkeypatch.setattr(step_log, "durable", lost)
    with pytest.raises(LoopStoppedError, match="TimeoutError"):
        loop.run(seed_tree, "true")
    monkeypatch.undo()
    with pytest.raises(LoopStoppedError):
        loop.run(seed_tree, "true")
    assert boxes.opened == 1
    assert loop._logs[ROOT_AGENT_ID].agent.next_seq == 1
    step_log.durable(step_id_for(RUN_ID, ROOT_AGENT_ID, 0), 1)
    assert [seq for seq, _, _ in _steps(engine)] == [0]


@pytest.mark.parametrize("call", ["run", "score"])
def test_a_tree_the_run_cannot_see_stops_the_loop_before_a_box(
    call: str, owner: tuple[User, int], engine: Engine, step_log: StepLogStore, trees: TreeStore, tmp_path: Path
) -> None:
    """A missing tree fails on every call, so the loop keeps a stop and opens no box.

    Args:
        call: The box step: ``run`` or ``score``.
    """
    metric_tree = _tree(trees, "metric.txt", "len")
    loop, _, boxes = _loop(owner, engine, step_log, trees, tmp_path, generation=1)

    for _ in range(2):
        with pytest.raises(LoopStoppedError):
            if call == "run":
                loop.run("missing-tree", "true")
            else:
                loop.score("missing-tree", metric_tree, _METRIC)
    assert loop._stopped is not None
    assert boxes.opened == 0
    assert _steps(engine) == []


def test_two_calls_open_one_agent_once(
    owner: tuple[User, int],
    engine: Engine,
    step_log: StepLogStore,
    trees: TreeStore,
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """A second call that comes while the agent opens waits for that open, and does not open it again."""
    seed_tree = _tree(trees, "README", "seed")
    first, _, _ = _loop(owner, engine, step_log, trees, tmp_path, generation=1)
    first.run(seed_tree, "printf a > answer.txt")
    entered, release = threading.Event(), threading.Event()
    reads: list[Any] = []
    read = engine_loop.last_committed_step

    def slow(*args: Any) -> Any:
        """Read the agent's last step slowly: wait until the test lets the read end.

        Args:
            *args: The read's arguments.

        Returns:
            What the real read returns.
        """
        reads.append(args)
        entered.set()
        release.wait(10)
        return read(*args)

    monkeypatch.setattr(engine_loop, "last_committed_step", slow)
    _new_generation(engine)
    loop, _, _ = _loop(owner, engine, step_log, trees, tmp_path, generation=2)
    results: list[Any] = []

    def call() -> None:
        """Run the committed command; the first call replays it and the second runs it again."""
        try:
            results.append(loop.run(seed_tree, "printf a > answer.txt")[0].exit_code)
        except BaseException as error:
            results.append(error)

    callers = [threading.Thread(target=call) for _ in range(2)]
    callers[0].start()
    assert entered.wait(10)
    callers[1].start()
    callers[1].join(0.5)
    release.set()
    for caller in callers:
        caller.join(10)

    assert len(reads) == 1
    assert results == [0, 0]
    assert [seq for seq, _, _ in _steps(engine)] == [0, 1]


_TOOLS: list[ToolDefinition] = [{"name": "edit", "description": "Edit the text.", "input_schema": {"type": "object"}}]


def test_a_model_call_with_tools_commits_its_tool_calls_and_cost_and_a_resume_replays_them(
    owner: tuple[User, int], engine: Engine, step_log: StepLogStore, trees: TreeStore, tmp_path: Path
) -> None:
    """The context's model call gives the tool calls and the cost, with no box open, and a resume repeats no call."""
    first, model, _ = _loop(owner, engine, step_log, trees, tmp_path, generation=1)
    ctx = first.context()
    assert ctx.call_model is not None
    reply = ctx.call_model("improve seed", _TOOLS)

    assert reply == ModelReply("seedx", Decimal(1), [{"id": "call_0", "name": "edit", "arguments": {"text": "seedx"}}])
    assert model.tools == [_TOOLS]
    assert model.open_boxes == [0]
    assert [kind for _, kind, _ in _steps(engine)] == ["model"]

    _new_generation(engine)
    resumed, resumed_model, _ = _loop(owner, engine, step_log, trees, tmp_path, generation=2)
    assert resumed.call_model("improve seed", _TOOLS) == reply
    assert resumed_model.prompts == []


def test_converted_openai_tool_calls_commit_and_a_resume_replays_them_unchanged(
    owner: tuple[User, int], engine: Engine, step_log: StepLogStore, trees: TreeStore, tmp_path: Path
) -> None:
    """Calls from a real OpenAI-shaped reply, also one with bad JSON, survive the step log as the live call gave
    them."""
    first, model, _ = _loop(owner, engine, step_log, trees, tmp_path, generation=1)
    message = {
        "role": "assistant",
        "content": None,
        "tool_calls": [
            {"id": "call_a", "type": "function", "function": {"name": "edit", "arguments": '{"text": "seedx"}'}},
            {"id": "call_b", "type": "function", "function": {"name": "edit", "arguments": '{"text": '}},
        ],
    }
    model.reply_calls = tool_calls_from_openai(message)
    reply = first.call_model("improve seed", _TOOLS)

    assert reply.tool_calls == [
        {"id": "call_a", "name": "edit", "arguments": {"text": "seedx"}},
        {"id": "call_b", "name": "edit", "arguments": '{"text": '},
    ]
    _new_generation(engine)
    resumed, resumed_model, _ = _loop(owner, engine, step_log, trees, tmp_path, generation=2)
    assert resumed.call_model("improve seed", _TOOLS) == reply
    assert resumed_model.prompts == []


def _nested(levels: int) -> dict[str, Any]:
    """Build tool call arguments nested to a depth, with no recursion.

    Args:
        levels: The number of nested objects, the outer one included.

    Returns:
        ``{"a": {"a": ... {}}}``, ``levels`` objects deep.
    """
    value: dict[str, Any] = {}
    for _ in range(levels - 1):
        value = {"a": value}
    return value


def _nested_tuples(levels: int) -> dict[str, Any]:
    """Build tool call arguments that hold tuples nested to a depth, with no recursion.

    Args:
        levels: The number of nested tuples.

    Returns:
        ``{"a": ((... (),),)}``, with ``levels`` tuples under the outer object.
    """
    value: tuple[Any, ...] = ()
    for _ in range(levels - 1):
        value = (value,)
    return {"a": value}


# An in-process model client can give tool calls that hold tuples, which the JSON encoder also walks recursively.
@pytest.mark.parametrize("arguments", [_nested(2000), _nested_tuples(2000)], ids=["objects", "tuples"])
def test_tool_arguments_nested_past_the_bound_commit_as_refused_text_and_a_resume_replays_them(
    arguments: dict[str, Any],
    owner: tuple[User, int],
    engine: Engine,
    step_log: StepLogStore,
    trees: TreeStore,
    tmp_path: Path,
) -> None:
    """Arguments too deep for the commit's JSON encoder fail only their call: the reply commits, with its cost.

    Args:
        arguments: Tool call arguments nested 2000 levels, in objects or in tuples.
        owner: Alice and her budget generation.
        engine: The database.
        step_log: The step log.
        trees: The tree store.
        tmp_path: Pytest's temporary folder.
    """
    first, model, _ = _loop(owner, engine, step_log, trees, tmp_path, generation=1)
    # 2000 levels is past Python's default recursion limit, so the commit's JSON encoder would raise RecursionError.
    model.reply_calls = [
        {"id": "call_0", "name": "edit", "arguments": arguments},
        {"id": "call_1", "name": "edit", "arguments": {"text": "seedx"}},
    ]
    reply = first.call_model("improve seed", _TOOLS)

    assert reply.cost_cents == Decimal(1)
    assert reply.tool_calls == [
        {"id": "call_0", "name": "edit", "arguments": TOO_DEEP_ARGUMENTS},
        {"id": "call_1", "name": "edit", "arguments": {"text": "seedx"}},
    ]
    committed = step_log.get_step(step_id_for(RUN_ID, ROOT_AGENT_ID, 0))
    assert "error" not in committed["body"]
    assert Decimal(committed["body"]["cost_cents"]) == 1
    _new_generation(engine)
    resumed, resumed_model, _ = _loop(owner, engine, step_log, trees, tmp_path, generation=2)
    assert resumed.call_model("improve seed", _TOOLS) == reply
    assert resumed_model.prompts == []


# 900 levels parses with no RecursionError at a shallow stack, but a later read at a deeper stack, for example
# LiteLLM's Anthropic conversion, can raise and fail every later model call.
@pytest.mark.parametrize(
    "arguments",
    ["[" * 900 + "]" * 900, '{"a": ' * 900 + "1" + "}" * 900, "[" * 900],
    ids=["array", "object", "bad-json"],
)
def test_deep_tool_argument_text_commits_as_refused_text_whatever_the_stack(
    arguments: str,
    owner: tuple[User, int],
    engine: Engine,
    step_log: StepLogStore,
    trees: TreeStore,
    tmp_path: Path,
) -> None:
    """Argument text that opens more levels than the bound fails only its call; shallow text that is no JSON stays.

    Args:
        arguments: Tool call argument text that opens 900 levels.
        owner: Alice and her budget generation.
        engine: The database.
        step_log: The step log.
        trees: The tree store.
        tmp_path: Pytest's temporary folder.
    """
    first, model, _ = _loop(owner, engine, step_log, trees, tmp_path, generation=1)
    model.reply_calls = [
        {"id": "call_0", "name": "edit", "arguments": arguments},
        {"id": "call_1", "name": "edit", "arguments": '{"text": '},
    ]
    reply = first.call_model("improve seed", _TOOLS)

    assert reply.cost_cents == Decimal(1)
    assert reply.tool_calls == [
        {"id": "call_0", "name": "edit", "arguments": TOO_DEEP_ARGUMENTS},
        {"id": "call_1", "name": "edit", "arguments": '{"text": '},
    ]
    _new_generation(engine)
    resumed, resumed_model, _ = _loop(owner, engine, step_log, trees, tmp_path, generation=2)
    assert resumed.call_model("improve seed", _TOOLS) == reply
    assert resumed_model.prompts == []


def test_tool_argument_text_at_the_depth_bound_commits_and_one_level_deeper_commits_as_refused_text(
    owner: tuple[User, int], engine: Engine, step_log: StepLogStore, trees: TreeStore, tmp_path: Path
) -> None:
    """Argument text that opens exactly the arguments' bound of levels stays; one level more is refused.

    Args:
        owner: Alice and her budget generation.
        engine: The database.
        step_log: The step log.
        trees: The tree store.
        tmp_path: Pytest's temporary folder.
    """
    first, model, _ = _loop(owner, engine, step_log, trees, tmp_path, generation=1)
    # A literal, not the constant, so a change to the bound fails this test.
    levels = 97
    at_bound = "[" * levels + "]" * levels
    model.reply_calls = [
        {"id": "call_0", "name": "edit", "arguments": at_bound},
        {"id": "call_1", "name": "edit", "arguments": f"[{at_bound}]"},
    ]
    reply = first.call_model("improve seed", _TOOLS)

    assert [call["arguments"] for call in reply.tool_calls] == [at_bound, TOO_DEEP_ARGUMENTS]


@pytest.mark.parametrize("deep", [_nested(2000), _nested_tuples(2000)], ids=["objects", "tuples"])
def test_a_reply_nested_past_the_bound_outside_tool_arguments_is_a_failed_model_call_with_its_cost(
    deep: dict[str, Any],
    owner: tuple[User, int],
    engine: Engine,
    step_log: StepLogStore,
    trees: TreeStore,
    tmp_path: Path,
) -> None:
    """A deep part of the reply that is not tool call arguments gives the engine a failed call, not a stop.

    Args:
        deep: A value nested 2000 levels, in objects or in tuples.
        owner: Alice and her budget generation.
        engine: The database.
        step_log: The step log.
        trees: The tree store.
        tmp_path: Pytest's temporary folder.
    """
    first, model, _ = _loop(owner, engine, step_log, trees, tmp_path, generation=1)
    model.reply_calls = [{"id": "call_0", "name": deep, "arguments": {}}]  # type: ignore[typeddict-item]
    with pytest.raises(ModelCallFailedError, match="ModelReplyTooDeepError") as live:
        first.call_model("improve seed", _TOOLS)
    assert live.value.cost_cents == Decimal(1)
    model.reply_calls = None
    assert first.call_model("improve other", _TOOLS).text == "otherx"
    failed = step_log.get_step(step_id_for(RUN_ID, ROOT_AGENT_ID, 0))
    assert failed["body"]["error"] == "ModelReplyTooDeepError"
    assert Decimal(failed["body"]["cost_cents"]) == 1
    assert [kind for _, kind, _ in _steps(engine)] == ["model", "model"]

    _new_generation(engine)
    resumed, resumed_model, _ = _loop(owner, engine, step_log, trees, tmp_path, generation=2)
    with pytest.raises(ModelCallFailedError, match="ModelReplyTooDeepError") as replayed:
        resumed.call_model("improve seed", _TOOLS)
    assert replayed.value.cost_cents == Decimal(1)
    assert resumed.call_model("improve other", _TOOLS).text == "otherx"
    assert resumed_model.prompts == []


def test_a_reply_at_the_body_bound_outside_tool_arguments_commits_and_one_level_deeper_is_refused(
    owner: tuple[User, int], engine: Engine, step_log: StepLogStore, trees: TreeStore, tmp_path: Path
) -> None:
    """A body that nests exactly 100 levels outside tool call arguments commits; 101 levels give a failed call.

    Args:
        owner: Alice and her budget generation.
        engine: The database.
        step_log: The step log.
        trees: The tree store.
        tmp_path: Pytest's temporary folder.
    """
    first, model, _ = _loop(owner, engine, step_log, trees, tmp_path, generation=1)
    # The step body, its tool call list and the call sit above the name: 97 levels there make 100 in the body.
    # Literals, not the constants, so a change to the body bound fails this test.
    at_bound = [{"id": "call_0", "name": _nested(97), "arguments": {}}]
    model.reply_calls = at_bound  # type: ignore[assignment]
    assert first.call_model("improve seed", _TOOLS).tool_calls == at_bound
    model.reply_calls = [{"id": "call_0", "name": _nested(98), "arguments": {}}]  # type: ignore[typeddict-item]
    with pytest.raises(ModelCallFailedError, match="ModelReplyTooDeepError"):
        first.call_model("improve other", _TOOLS)


def _deep_tuple_keys(levels: int) -> dict[Any, int]:
    """Build an object whose two keys are different tuples nested to a depth, with no recursion.

    Args:
        levels: The number of nested tuples in each key.

    Returns:
        ``{((...(0,)...),): 0, ((...(1,)...),): 1}``; the keys differ only at their deepest level.
    """
    keys: list[Any] = [(0,), (1,)]
    for _ in range(levels - 1):
        keys = [(key,) for key in keys]
    return {keys[0]: 0, keys[1]: 1}


def test_deep_tuple_keys_fail_only_their_call_or_their_reply_and_the_cost_is_billed(
    owner: tuple[User, int], engine: Engine, step_log: StepLogStore, trees: TreeStore, tmp_path: Path
) -> None:
    """Deep tuple keys make the encoder's key sort recurse; they give a refusal with its cost, not a loop stop.

    Args:
        owner: Alice and her budget generation.
        engine: The database.
        step_log: The step log.
        trees: The tree store.
        tmp_path: Pytest's temporary folder.
    """
    first, model, _ = _loop(owner, engine, step_log, trees, tmp_path, generation=1)
    # 3000 levels is past Python's default recursion limit, so a sort of the two keys would raise RecursionError.
    model.reply_calls = [
        {"id": "call_0", "name": "edit", "arguments": _deep_tuple_keys(3000)},
        {"id": "call_1", "name": "edit", "arguments": {"text": "seedx"}},
    ]
    reply = first.call_model("improve seed", _TOOLS)
    assert reply.tool_calls == [
        {"id": "call_0", "name": "edit", "arguments": TOO_DEEP_ARGUMENTS},
        {"id": "call_1", "name": "edit", "arguments": {"text": "seedx"}},
    ]
    assert reply.cost_cents == Decimal(1)

    model.reply_calls = [{"id": "call_0", "name": _deep_tuple_keys(3000), "arguments": {}}]  # type: ignore[typeddict-item]
    with pytest.raises(ModelCallFailedError, match="ModelReplyTooDeepError") as live:
        first.call_model("improve other", _TOOLS)
    assert live.value.cost_cents == Decimal(1)
    failed = step_log.get_step(step_id_for(RUN_ID, ROOT_AGENT_ID, 1))
    assert Decimal(failed["body"]["cost_cents"]) == 1
    model.reply_calls = None
    assert first.call_model("improve other", _TOOLS).text == "otherx"
    assert first.remaining_cost_usd() == pytest.approx(9.97)


class _RecursiveKey:
    """A key whose order comparison recurses without end, as a key type that the depth walk does not enter can."""

    def __lt__(self, other: object) -> bool:
        """Compare by comparing again, which raises :class:`RecursionError`.

        Args:
            other: The other key.

        Returns:
            Never; the comparison recurses until Python stops it.
        """
        return self < other


def test_a_reply_whose_key_sort_recurses_is_a_failed_model_call_with_its_cost(
    owner: tuple[User, int], engine: Engine, step_log: StepLogStore, trees: TreeStore, tmp_path: Path
) -> None:
    """A :class:`RecursionError` in the commit's key sort gives a refused reply that the lease bills.

    Args:
        owner: Alice and her budget generation.
        engine: The database.
        step_log: The step log.
        trees: The tree store.
        tmp_path: Pytest's temporary folder.
    """
    first, model, _ = _loop(owner, engine, step_log, trees, tmp_path, generation=1)
    model.reply_calls = [{"id": "call_0", "name": "edit", "arguments": {_RecursiveKey(): 0, _RecursiveKey(): 1}}]
    with pytest.raises(ModelCallFailedError, match="ModelReplyNotJSONError") as live:
        first.call_model("improve seed", _TOOLS)
    assert live.value.cost_cents == Decimal(1)
    model.reply_calls = None
    assert first.call_model("improve other", _TOOLS).text == "otherx"


def test_too_deep_arguments_is_a_shallow_json_object_with_no_command() -> None:
    """The text must be a flat JSON object with no ``command``, so a provider accepts it and an engine refuses it."""
    value = json.loads(TOO_DEEP_ARGUMENTS)
    assert isinstance(value, dict)
    assert "command" not in value
    assert all(not isinstance(item, (dict, list)) for item in value.values())


def test_tool_arguments_at_the_depth_bound_commit_and_one_level_deeper_commit_as_refused_text(
    owner: tuple[User, int], engine: Engine, step_log: StepLogStore, trees: TreeStore, tmp_path: Path
) -> None:
    """Arguments that fill the body to exactly 100 levels commit as they are; deeper ones are refused.

    Args:
        owner: Alice and her budget generation.
        engine: The database.
        step_log: The step log.
        trees: The tree store.
        tmp_path: Pytest's temporary folder.
    """
    first, model, _ = _loop(owner, engine, step_log, trees, tmp_path, generation=1)
    # The step body, its tool call list and the call hold the arguments: three levels above them.
    arguments = _nested(97)
    model.reply_calls = [{"id": "call_0", "name": "edit", "arguments": arguments}]
    reply = first.call_model("improve seed", _TOOLS)
    assert reply.tool_calls == model.reply_calls
    model.reply_calls = [{"id": "call_0", "name": "edit", "arguments": {"a": arguments}}]
    deeper = first.call_model("improve other", _TOOLS)
    assert deeper.tool_calls == [{"id": "call_0", "name": "edit", "arguments": TOO_DEEP_ARGUMENTS}]

    _new_generation(engine)
    resumed, resumed_model, _ = _loop(owner, engine, step_log, trees, tmp_path, generation=2)
    assert resumed.call_model("improve seed", _TOOLS) == reply
    assert resumed.call_model("improve other", _TOOLS) == deeper
    assert resumed_model.prompts == []


def test_a_reply_larger_than_the_bound_is_a_failed_model_call_with_its_cost(
    owner: tuple[User, int],
    engine: Engine,
    step_log: StepLogStore,
    trees: TreeStore,
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """A reply past ``_MAX_REPLY_BYTES`` gives a failed call with its cost, and the run goes on.

    Args:
        owner: Alice and her budget generation.
        engine: The database.
        step_log: The step log.
        trees: The tree store.
        tmp_path: Pytest's temporary folder.
        monkeypatch: Pytest's monkeypatch, to make the bound small.
    """
    monkeypatch.setattr(engine_loop, "_MAX_REPLY_BYTES", 1000)
    first, model, _ = _loop(owner, engine, step_log, trees, tmp_path, generation=1)
    model.reply_calls = [{"id": "call_0", "name": "edit", "arguments": {"text": "y" * 1000}}]
    with pytest.raises(ModelCallFailedError, match="ModelReplyTooLargeError") as live:
        first.call_model("improve seed", _TOOLS)
    assert live.value.cost_cents == Decimal(1)
    model.reply_calls = None
    assert first.call_model("improve other", _TOOLS).text == "otherx"


def test_the_size_bound_measures_the_bytes_that_the_commit_writes(
    owner: tuple[User, int],
    engine: Engine,
    step_log: StepLogStore,
    trees: TreeStore,
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """A reply whose committed JSON, cost field included, is exactly ``_MAX_REPLY_BYTES`` commits; one byte more fails.

    Args:
        owner: Alice and her budget generation.
        engine: The database.
        step_log: The step log.
        trees: The tree store.
        tmp_path: Pytest's temporary folder.
        monkeypatch: Pytest's monkeypatch, to set the bound.
    """
    first, _, _ = _loop(owner, engine, step_log, trees, tmp_path, generation=1)
    reply = first.call_model("improve seed", _TOOLS)
    committed = step_log.get_step(step_id_for(RUN_ID, ROOT_AGENT_ID, 0))["body"]
    assert "cost_cents" in committed
    size = len(step_log_store._encode_body(committed))
    monkeypatch.setattr(engine_loop, "_MAX_REPLY_BYTES", size)
    assert first.call_model("improve seed", _TOOLS) == reply
    assert step_log.get_step(step_id_for(RUN_ID, ROOT_AGENT_ID, 1))["body"] == committed
    monkeypatch.setattr(engine_loop, "_MAX_REPLY_BYTES", size - 1)
    with pytest.raises(ModelCallFailedError, match="ModelReplyTooLargeError"):
        first.call_model("improve seed", _TOOLS)


@pytest.mark.parametrize(
    "arguments",
    # A set is no JSON value at all. A lone surrogate is no case here: SKY-821 escapes it, so the step commits.
    [{"items": {1, 2}}],
    ids=["set"],
)
def test_a_reply_that_is_not_utf8_json_is_a_failed_model_call_with_its_cost_and_a_resume_replays_it(
    owner: tuple[User, int],
    engine: Engine,
    step_log: StepLogStore,
    trees: TreeStore,
    tmp_path: Path,
    arguments: dict[str, Any],
) -> None:
    """A reply that the commit cannot encode gives a failed call that the lease bills, not a stop with a lost cost.

    Args:
        owner: Alice and her budget generation.
        engine: The database.
        step_log: The step log.
        trees: The tree store.
        tmp_path: Pytest's temporary folder.
        arguments: Tool call arguments that are not UTF-8 JSON.
    """
    first, model, _ = _loop(owner, engine, step_log, trees, tmp_path, generation=1)
    model.reply_calls = [{"id": "call_0", "name": "edit", "arguments": arguments}]
    with pytest.raises(ModelCallFailedError, match="ModelReplyNotJSONError") as live:
        first.call_model("improve seed", _TOOLS)
    assert live.value.cost_cents == Decimal(1)
    failed = step_log.get_step(step_id_for(RUN_ID, ROOT_AGENT_ID, 0))
    assert failed["body"]["error"] == "ModelReplyNotJSONError"
    assert Decimal(failed["body"]["cost_cents"]) == 1
    assert first.remaining_cost_usd() == pytest.approx(9.99)
    model.reply_calls = None
    assert first.call_model("improve other", _TOOLS).text == "otherx"

    _new_generation(engine)
    resumed, resumed_model, _ = _loop(owner, engine, step_log, trees, tmp_path, generation=2)
    with pytest.raises(ModelCallFailedError, match="ModelReplyNotJSONError") as replayed:
        resumed.call_model("improve seed", _TOOLS)
    assert replayed.value.cost_cents == Decimal(1)
    assert resumed_model.prompts == []


def test_a_tool_only_reply_with_no_text_gives_an_empty_text_live_and_on_a_resume(
    owner: tuple[User, int],
    engine: Engine,
    step_log: StepLogStore,
    trees: TreeStore,
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """An OpenAI-shaped tool-only reply has ``content: None``; the engine must get "", never the text "None".

    Args:
        monkeypatch: Replaces the loop's model with one that gives a tool-only reply.
    """
    first, _, _ = _loop(owner, engine, step_log, trees, tmp_path, generation=1)
    calls = tool_calls_from_openai(
        {
            "role": "assistant",
            "content": None,
            "tool_calls": [{"id": "call_a", "type": "function", "function": {"name": "edit", "arguments": "{}"}}],
        }
    )

    def tool_only(prompt: str | list[dict[str, Any]], tools: list[ToolDefinition] | None) -> ModelReply:
        """Give a reply with tool calls and no text, as a converted OpenAI message gives it.

        Args:
            prompt: The prompt.
            tools: The tool definitions.

        Returns:
            The reply, with ``None`` as its text.
        """
        return ModelReply(None, Decimal(1), calls)  # type: ignore[arg-type]

    monkeypatch.setattr(first, "_model", tool_only)
    reply = first.call_model("improve seed", _TOOLS)

    assert reply.text == ""
    assert step_log.get_step(step_id_for(RUN_ID, ROOT_AGENT_ID, 0))["body"]["text"] == ""
    _new_generation(engine)
    resumed, resumed_model, _ = _loop(owner, engine, step_log, trees, tmp_path, generation=2)
    assert resumed.call_model("improve seed", _TOOLS) == reply
    assert resumed_model.prompts == []


def test_a_reply_with_lone_surrogates_commits_and_the_run_goes_on_and_a_resume_replays_it(
    owner: tuple[User, int],
    engine: Engine,
    step_log: StepLogStore,
    trees: TreeStore,
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """A lone surrogate is no UTF-8, so a reply that holds one must not stop the run at its commit (SKY-821).

    Arguments that parse to a surrogate keep their raw text, so the engine refuses only that call. Surrogates in
    the text, the tool name and arguments that a client already parsed become escapes.

    Args:
        monkeypatch: Replaces the loop's model with one that gives the bad reply first.
    """
    first, model, _ = _loop(owner, engine, step_log, trees, tmp_path, generation=1)
    calls = tool_calls_from_openai(
        {
            "role": "assistant",
            "content": None,
            "tool_calls": [
                {"id": "call_a", "type": "function", "function": {"name": "edit", "arguments": '{"text":"\\ud800"}'}},
                {"id": "call_b", "type": "function", "function": {"name": "ed\udfffit", "arguments": "{}"}},
            ],
        }
    )

    def bad(prompt: str | list[dict[str, Any]], tools: list[ToolDefinition] | None) -> ModelReply:
        """Give a reply with lone surrogates in its text and in a parsed argument key, then answer as the fake.

        Args:
            prompt: The prompt.
            tools: The tool definitions.

        Returns:
            The bad reply for the first prompt; the fake model's reply after it.
        """
        if prompt != "improve seed":
            return model(prompt, tools)
        parsed: ToolCall = {"id": "call_c", "name": "edit", "arguments": {"k\ud83d": ["\udc00"]}}
        return ModelReply("bad \ud800 text", Decimal(1), [*calls, parsed])

    monkeypatch.setattr(first, "_model", bad)
    reply = first.call_model("improve seed", _TOOLS)
    after = first.call_model("go on", _TOOLS)

    assert reply == ModelReply(
        "bad \\ud800 text",
        Decimal(1),
        [
            {"id": "call_a", "name": "edit", "arguments": '{"text":"\\ud800"}'},
            {"id": "call_b", "name": "ed\\udfffit", "arguments": {}},
            {"id": "call_c", "name": "edit", "arguments": '{"k\\ud83d": ["\\udc00"]}'},
        ],
    )
    assert after.text == "onx"
    assert [kind for _, kind, _ in _steps(engine)] == ["model", "model"]
    _new_generation(engine)
    resumed, resumed_model, _ = _loop(owner, engine, step_log, trees, tmp_path, generation=2)
    assert resumed.call_model("improve seed", _TOOLS) == reply
    assert resumed.call_model("go on", _TOOLS) == after
    assert resumed_model.prompts == []


def test_a_committed_model_step_with_a_null_text_replays_an_empty_text() -> None:
    """A record that an older build committed with a null text replays "", as a new record does."""
    reply = engine_loop._reply({"body": {"text": None, "cost_cents": "1"}})

    assert reply == ModelReply("", Decimal(1), [])


@pytest.mark.parametrize("tools", [None, [{"name": "other", "description": "", "input_schema": {}}]])
def test_a_resume_with_other_tools_diverges(
    tools: list[ToolDefinition] | None,
    owner: tuple[User, int],
    engine: Engine,
    step_log: StepLogStore,
    trees: TreeStore,
    tmp_path: Path,
) -> None:
    """The tools are part of the prompt hash, so the same prompt with other tools is another call.

    Args:
        tools: The resume's tools, which differ from the live call's tools.
    """
    first, _, _ = _loop(owner, engine, step_log, trees, tmp_path, generation=1)
    first.call_model("improve seed", _TOOLS)

    _new_generation(engine)
    resumed, resumed_model, _ = _loop(owner, engine, step_log, trees, tmp_path, generation=2)
    with pytest.raises(StepDivergedError):
        resumed.call_model("improve seed", tools)
    assert resumed_model.prompts == []


def test_the_live_cost_is_the_committed_cost_that_a_resume_gives(
    owner: tuple[User, int], engine: Engine, step_log: StepLogStore, trees: TreeStore, tmp_path: Path
) -> None:
    """The ledger rounds a cost up to nine fractional digits, and the live reply gives that rounded cost."""
    first, model, _ = _loop(owner, engine, step_log, trees, tmp_path, generation=1)
    model.cost = Decimal("0.0000000001")
    live = first.call_model("improve seed")
    assert live.cost_cents == Decimal("0.000000001")

    _new_generation(engine)
    resumed, _, _ = _loop(owner, engine, step_log, trees, tmp_path, generation=2)
    assert resumed.call_model("improve seed") == live


def test_the_context_runs_a_command_as_one_step_and_a_resume_replays_it(
    owner: tuple[User, int], engine: Engine, step_log: StepLogStore, trees: TreeStore, tmp_path: Path
) -> None:
    """The context's command call goes through the loop: one box, one step, and no box on the replay."""
    seed_tree = _tree(trees, "README", "seed")
    first, _, boxes = _loop(owner, engine, step_log, trees, tmp_path, generation=1)
    ctx = first.context()
    assert ctx.run_command is not None
    result, tree_id = ctx.run_command(seed_tree, "printf hi > out.txt", ())

    assert result.exit_code == 0
    assert boxes.opened == 1
    assert first.read_file(tree_id, "out.txt") == b"hi"
    assert [kind for _, kind, _ in _steps(engine)] == ["command"]

    _new_generation(engine)
    resumed, _, resumed_boxes = _loop(owner, engine, step_log, trees, tmp_path, generation=2)
    resumed_ctx = resumed.context()
    assert resumed_ctx.run_command is not None
    assert resumed_ctx.run_command(seed_tree, "printf hi > out.txt", ()) == (result, tree_id)
    assert resumed_boxes.opened == 0


def test_put_files_writes_a_tree_with_no_step_and_the_same_files_give_the_same_tree(
    owner: tuple[User, int], engine: Engine, step_log: StepLogStore, trees: TreeStore, tmp_path: Path
) -> None:
    """A tree write opens no box and commits no step; a resume that writes the same files gets the same tree."""
    first, _, boxes = _loop(owner, engine, step_log, trees, tmp_path, generation=1)
    ctx = first.context()
    assert ctx.put_files is not None
    assert ctx.read_file is not None
    base = ctx.put_files({"src/a.py": b"a", "README": b"r"}, None)
    tree = ctx.put_files({"src/b.py": b"b", "README": b"new"}, base)

    assert ctx.read_file(tree, "src/a.py") == b"a"
    assert ctx.read_file(tree, "src/b.py") == b"b"
    assert ctx.read_file(tree, "README") == b"new"
    assert ctx.read_file(base, "README") == b"r"
    assert ctx.read_file(tree, "src") is None
    assert ctx.read_file(tree, "missing/x.py") is None
    assert ctx.read_file(tree, "README/x") is None
    assert boxes.opened == 0
    assert _steps(engine) == []

    _new_generation(engine)
    resumed, _, _ = _loop(owner, engine, step_log, trees, tmp_path, generation=2)
    assert resumed.put_files({"README": b"r", "src/a.py": b"a"}) == base
    result, _ = resumed.run(tree, "cat src/a.py src/b.py")
    assert result.stdout == "ab"


def test_put_files_keeps_an_executable_and_refuses_bad_paths(
    owner: tuple[User, int], engine: Engine, step_log: StepLogStore, trees: TreeStore, tmp_path: Path
) -> None:
    """A rewritten executable stays executable; a file-and-folder path and an unseen base tree are refused."""
    loop, _, _ = _loop(owner, engine, step_log, trees, tmp_path, generation=1)
    base = trees.put_tree(RUN_ID, [TreeEntry("run.sh", "exec", trees.put_blob(RUN_ID, b"old"))])

    tree = loop.put_files({"run.sh": b"new"}, base)
    assert trees.get_tree(RUN_ID, tree) == [TreeEntry("run.sh", "exec", trees.put_blob(RUN_ID, b"new"))]
    with pytest.raises(ValueError, match="both a file and a folder"):
        loop.put_files({"a": b"1", "a/b": b"2"})
    with pytest.raises(ValueError, match="bad tree entry name"):
        loop.put_files({"a/../b": b"1"})
    with pytest.raises(LookupError, match="cannot see tree"):
        loop.put_files({"a": b"1"}, "0" * 64)


def test_the_remaining_cost_falls_by_the_cost_of_each_step(
    owner: tuple[User, int], engine: Engine, step_log: StepLogStore, trees: TreeStore, tmp_path: Path
) -> None:
    """The context reads the budget's room: 10 dollars, then 2 cents less after a 2-cent model step."""
    loop, model, boxes = _loop(owner, engine, step_log, trees, tmp_path, generation=1)
    ctx = loop.context()
    assert ctx.remaining_cost_usd is not None
    assert ctx.remaining_cost_usd() == pytest.approx(10.0)
    model.cost = Decimal(2)
    loop.model("improve seed")
    assert ctx.remaining_cost_usd() == pytest.approx(9.98)
    assert [kind for _, kind, _ in _steps(engine)] == ["command", "model", "command"]
    assert boxes.opened == 0


def _spend_while_room(loop: EngineLoop) -> list[str]:
    """Ask the model while the budget's room is above 9.97 dollars, as an engine that stops on its budget does.

    Args:
        loop: The loop.

    Returns:
        The model replies.
    """
    replies = []
    while loop.remaining_cost_usd() > 9.97:
        replies.append(loop.model(f"improve {len(replies)}"))
    return replies


def test_a_resume_replays_the_committed_room_and_stops_where_the_live_run_stopped(
    owner: tuple[User, int], engine: Engine, step_log: StepLogStore, trees: TreeStore, tmp_path: Path
) -> None:
    """The room read is a committed step: a resume gets the live room, not the room after the live spend."""
    first, model, _ = _loop(owner, engine, step_log, trees, tmp_path, generation=1)
    model.cost = Decimal(2)
    live = _spend_while_room(first)
    assert len(live) == 2

    _new_generation(engine)
    resumed, resumed_model, boxes = _loop(owner, engine, step_log, trees, tmp_path, generation=2)
    resumed_model.cost = Decimal(2)
    assert _spend_while_room(resumed) == live
    assert resumed_model.prompts == []
    assert boxes.opened == 0


def test_the_context_carries_the_repo_seed_of_its_caller(
    owner: tuple[User, int], engine: Engine, step_log: StepLogStore, trees: TreeStore, tmp_path: Path
) -> None:
    """The caller of the context passes the seed tree and its path rules, and the engine reads the seed with no box."""
    hidden = hidden_hashes(["metric.py"])
    loop, _, boxes = _loop(owner, engine, step_log, trees, tmp_path, generation=1, hidden=hidden)
    seed = RepoSeed(
        _store(trees, {"README": b"seed", "metric.py": b"secret"}),
        editable_paths=("src",),
        readonly_paths=("tests",),
        constrained_paths=("config.yaml",),
        hidden_hashes=hidden,
    )
    ctx = loop.context(repo_seed=seed)
    assert ctx.repo_seed == replace(seed, tree_id=ctx.repo_seed.tree_id)
    assert ctx.read_file is not None
    assert ctx.read_file(seed.tree_id, "README") == b"seed"
    assert ctx.read_file(seed.tree_id, "metric.py") is None
    assert boxes.opened == 0


def test_the_engine_gets_the_visible_seed_tree_id_and_runs_steps_from_it(
    owner: tuple[User, int], engine: Engine, step_log: StepLogStore, trees: TreeStore, tmp_path: Path
) -> None:
    """The raw seed tree id is a hash of the hidden files too, so the engine gets the id of the visible tree.

    With no hidden path in the seed, the id stays the same. Steps run from the visible id as from the seed.
    """
    hidden = hidden_hashes(["metric.py"])
    loop, _, boxes = _loop(owner, engine, step_log, trees, tmp_path, generation=1, hidden=hidden)
    raw = _store(trees, {"README": b"seed", "metric.py": b"secret"})
    ctx = loop.context(repo_seed=RepoSeed(raw, hidden_hashes=hidden))
    assert ctx.repo_seed is not None
    assert ctx.list_files is not None
    assert ctx.run_command is not None
    visible = ctx.repo_seed.tree_id

    assert visible != raw
    assert visible == _store(trees, {"README": b"seed"})
    assert ctx.list_files(visible) == ["README"]
    from_visible, after = ctx.run_command(visible, "cat README; ls", ())
    from_raw, _ = ctx.run_command(raw, "cat README; ls", ())
    assert from_visible.stdout == from_raw.stdout
    assert "seed" in from_visible.stdout
    assert "metric.py" not in from_visible.stdout
    assert ctx.list_files(after) == ["README"]
    assert boxes.opened == 2

    plain = _store(trees, {"README": b"seed"})
    assert loop.context(repo_seed=RepoSeed(plain, hidden_hashes=hidden)).repo_seed == RepoSeed(
        plain, hidden_hashes=hidden
    )


@pytest.mark.parametrize("seed_hidden", [{}, hidden_hashes(["other.py"])])
def test_a_seed_that_hides_other_paths_than_the_loop_is_refused(
    owner: tuple[User, int],
    engine: Engine,
    step_log: StepLogStore,
    trees: TreeStore,
    tmp_path: Path,
    seed_hidden: dict[str, str],
) -> None:
    """The loop owns its hidden paths: a context seed with other hashes cannot narrow or change them.

    Args:
        seed_hidden: The seed's hidden-path hashes, which differ from the loop's.
    """
    loop, _, _ = _loop(owner, engine, step_log, trees, tmp_path, generation=1, hidden=hidden_hashes(["metric.py"]))
    with pytest.raises(ValueError, match="hides other paths"):
        loop.context(repo_seed=RepoSeed(_tree(trees, "README", "seed"), hidden_hashes=seed_hidden))


def test_no_context_call_can_pass_its_own_hidden_paths(
    owner: tuple[User, int], engine: Engine, step_log: StepLogStore, trees: TreeStore, tmp_path: Path
) -> None:
    """The context's file and command calls take no hidden-path argument, so an engine cannot turn the filter off."""
    loop, _, boxes = _loop(owner, engine, step_log, trees, tmp_path, generation=1, hidden=hidden_hashes(["eval"]))
    tree = _store(trees, {"eval/metric.py": b"secret"})
    ctx = loop.context()
    assert ctx.read_file is not None
    assert ctx.list_files is not None
    assert ctx.run_command is not None
    assert ctx.file_ids is not None
    for call, args in [
        (ctx.read_file, (tree, "eval/metric.py")),
        (ctx.list_files, (tree, "eval")),
        (ctx.run_command, (tree, "cat eval/metric.py", ())),
        (ctx.file_ids, (tree,)),
    ]:
        with pytest.raises(TypeError):
            call(*args, hidden_hashes={})
    assert boxes.opened == 0


def test_list_files_lists_a_folder_and_gives_none_for_a_path_that_is_not_a_folder(
    owner: tuple[User, int], engine: Engine, step_log: StepLogStore, trees: TreeStore, tmp_path: Path
) -> None:
    """A listing gives the sorted file paths from the root, at any depth, with no step and no box."""
    loop, _, boxes = _loop(owner, engine, step_log, trees, tmp_path, generation=1)
    ctx = loop.context()
    assert ctx.list_files is not None
    tree = loop.put_files({"src/b.py": b"b", "src/lib/a.py": b"a", "README": b"r"})

    assert ctx.list_files(tree, "") == ["README", "src/b.py", "src/lib/a.py"]
    assert ctx.list_files(tree, ".") == ["README", "src/b.py", "src/lib/a.py"]
    assert ctx.list_files(tree, "src") == ["src/b.py", "src/lib/a.py"]
    assert ctx.list_files(tree, "src/lib") == ["src/lib/a.py"]
    assert ctx.list_files(tree, "README") is None
    assert ctx.list_files(tree, "README/x") is None
    assert ctx.list_files(tree, "missing") is None
    assert _steps(engine) == []
    assert boxes.opened == 0


def test_file_ids_give_equal_ids_for_equal_bytes_and_leave_out_hidden_paths(
    owner: tuple[User, int], engine: Engine, step_log: StepLogStore, trees: TreeStore, tmp_path: Path
) -> None:
    """The content ids tell changed files apart with no read, no step and no box, and hide what the loop hides."""
    loop, _, boxes = _loop(owner, engine, step_log, trees, tmp_path, generation=1, hidden=hidden_hashes(["eval/"]))
    tree = _store(trees, {"eval/metric.py": b"m", "src/a.py": b"a", "src/lib/b.py": b"b", ".optimize/exploits/x": b"x"})
    ctx = loop.context()
    assert ctx.file_ids is not None
    after = loop.put_files({"src/a.py": b"changed"}, tree)

    before_ids, after_ids = ctx.file_ids(tree), ctx.file_ids(after)

    assert sorted(before_ids) == sorted(after_ids) == ["src/a.py", "src/lib/b.py"]
    assert before_ids["src/lib/b.py"] == after_ids["src/lib/b.py"]
    assert before_ids["src/a.py"] != after_ids["src/a.py"]
    assert _steps(engine) == []
    assert boxes.opened == 0


def test_the_context_hides_the_hidden_paths_of_the_seed(
    owner: tuple[User, int], engine: Engine, step_log: StepLogStore, trees: TreeStore, tmp_path: Path
) -> None:
    """A hidden folder hides its whole subtree from list_files and read_file; its key is a path hash."""
    hidden = hidden_hashes(["eval/", "optimize.toml"])
    assert sorted(hidden) == sorted(hashlib.sha256(path.encode()).hexdigest() for path in ["eval", "optimize.toml"])
    loop, _, _ = _loop(owner, engine, step_log, trees, tmp_path, generation=1, hidden=hidden)
    tree = _store(trees, {"eval/metric.py": b"m", "eval/data/x.json": b"x", "src/a.py": b"a", "optimize.toml": b"t"})
    ctx = loop.context(repo_seed=RepoSeed(tree, hidden_hashes=hidden))
    assert ctx.list_files is not None
    assert ctx.read_file is not None

    assert ctx.list_files(tree, "") == ["src/a.py"]
    assert ctx.list_files(tree, "eval") is None
    assert ctx.list_files(tree, "eval/data") is None
    assert ctx.list_files(tree, "missing") is None
    assert ctx.read_file(tree, "eval/data/x.json") is None
    assert ctx.read_file(tree, "optimize.toml") is None
    assert ctx.read_file(tree, "src/a.py") == b"a"


def test_a_candidate_box_gets_no_hidden_path_and_no_exploits(
    owner: tuple[User, int], engine: Engine, step_log: StepLogStore, trees: TreeStore, tmp_path: Path
) -> None:
    """An engine command cannot read or list a hidden path or ``.optimize/exploits``; the metric box still can."""
    hidden = hidden_hashes(["eval", "optimize.toml"])
    loop, _, _ = _loop(owner, engine, step_log, trees, tmp_path, generation=1, hidden=hidden)
    tree = _store(
        trees,
        {
            "eval/metric.py": b"secret",
            "optimize.toml": b"secret",
            ".optimize/exploits/1/patch.diff": b"secret",
            ".optimize/notes.md": b"n",
            "src/a.py": b"a",
        },
    )
    ctx = loop.context(repo_seed=RepoSeed(tree, hidden_hashes=hidden))
    assert ctx.run_command is not None

    read, after = ctx.run_command(tree, "cat eval/metric.py optimize.toml .optimize/exploits/1/patch.diff", ())
    listed, _ = ctx.run_command(tree, "find . -type f | sort", ())

    assert "secret" not in read.stdout
    assert read.exit_code != 0
    assert listed.stdout.split() == ["./.optimize/notes.md", "./src/a.py"]
    assert loop.list_files(after) == [".optimize/notes.md", "src/a.py"]
    assert loop.score(_tree(trees, "answer.txt", "abc"), tree, "grep -c secret metric/eval/metric.py") == 1.0


def test_a_folder_with_only_hidden_files_looks_like_a_missing_folder(
    owner: tuple[User, int], engine: Engine, step_log: StepLogStore, trees: TreeStore, tmp_path: Path
) -> None:
    """With only ``eval/metric.py`` hidden, ``eval/`` is missing from list_files and from the candidate box."""
    hidden = hidden_hashes(["eval/metric.py"])
    loop, _, _ = _loop(owner, engine, step_log, trees, tmp_path, generation=1, hidden=hidden)
    tree = _store(trees, {"eval/metric.py": b"secret", "src/a.py": b"a"})
    ctx = loop.context(repo_seed=RepoSeed(tree, hidden_hashes=hidden))
    assert ctx.list_files is not None
    assert ctx.run_command is not None

    assert ctx.list_files(tree, "eval") is None
    assert ctx.list_files(tree, "eval") == ctx.list_files(tree, "missing")
    assert ctx.list_files(tree, "") == ["src/a.py"]
    listed, _ = ctx.run_command(tree, "find . | sort; ls eval", ())

    assert listed.stdout.split() == [".", "./src", "./src/a.py"]
    assert listed.exit_code != 0


def test_listing_a_partly_hidden_folder_writes_no_object_and_works_after_the_step_log_closes(
    owner: tuple[User, int],
    engine: Engine,
    step_log: StepLogStore,
    trees: TreeStore,
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """A listing only reads: a folder that the filter thins out puts no tree object in the step log."""
    hidden = hidden_hashes(["eval/metric.py"])
    loop, _, _ = _loop(owner, engine, step_log, trees, tmp_path, generation=1, hidden=hidden)
    tree = _store(trees, {"eval/metric.py": b"secret", "eval/README.md": b"r", "src/a.py": b"a"})
    put: list[str] = []
    real_put = step_log.put_object

    def spy(run_id: str, object_id: str, data: bytes) -> None:
        """Record the object id, then put the object.

        Args:
            run_id: The run.
            object_id: The object's id.
            data: The object's bytes.
        """
        put.append(object_id)
        real_put(run_id, object_id, data)

    monkeypatch.setattr(step_log, "put_object", spy)

    assert loop.list_files(tree, "eval") == ["eval/README.md"]
    assert put == []
    step_log.close()
    assert loop.list_files(tree, "eval") == ["eval/README.md"]


def test_a_scorer_of_the_eval_server_cannot_read_a_hidden_path(
    owner: tuple[User, int], engine: Engine, step_log: StepLogStore, trees: TreeStore, tmp_path: Path
) -> None:
    """A step scorer gets the same hidden-path filter as the engine context: its candidate box has no hidden file."""
    loop, _, _ = _loop(owner, engine, step_log, trees, tmp_path, generation=1, hidden=hidden_hashes(["eval"]))
    seed_tree = _store(trees, {"eval/metric.py": b"secret", "README": b"r"})
    outputs: list[str] = []

    def scorer(loop: EngineLoop, candidate: Any, case: Any) -> tuple[float | None, dict[str, Any]]:
        """Write the candidate on the seed tree, then read the hidden file in the candidate box.

        Args:
            loop: The engine loop.
            candidate: The candidate text.
            case: Not used.

        Returns:
            No score and no side information.
        """
        tree = loop.put_files({"candidate.txt": str(candidate).encode()}, seed_tree)
        result, _ = loop.run(tree, "cat candidate.txt eval/metric.py")
        outputs.append(result.stdout)
        return None, {}

    loop.server(scorer, max_evals=1).evaluate("v1", {})
    assert outputs == ["v1"]


@pytest.mark.parametrize(
    ("hidden", "links", "secret"),
    [
        (["eval"], {"eval": "real"}, "real/answer.txt"),
        (["eval"], {"eval": "real.txt"}, "real.txt"),
        (["data/answer.txt"], {"data": "real"}, "real/answer.txt"),
        (["data/sub/answer.txt"], {"data": "real", "real/sub": "../other"}, "other/answer.txt"),
        (["data/eval"], {"data": "real", "real/eval/link": "../../other"}, "other/answer.txt"),
    ],
    ids=[
        "hidden-link",
        "hidden-link-to-a-file",
        "hidden-file-below-a-linked-folder",
        "hidden-file-below-a-chain-of-links",
        "link-in-a-hidden-folder-below-a-linked-folder",
    ],
)
def test_a_symlink_at_or_above_a_hidden_path_stops_the_loop(
    owner: tuple[User, int],
    engine: Engine,
    step_log: StepLogStore,
    trees: TreeStore,
    tmp_path: Path,
    hidden: list[str],
    links: dict[str, str],
    secret: str,
) -> None:
    """The filter matches paths, so a link's target would stay visible: the loop refuses the tree before any read.

    Args:
        hidden: The run's hidden paths.
        links: The symlink targets by path.
        secret: The real path of the hidden file that a link would show.
    """
    loop, _, boxes = _loop(owner, engine, step_log, trees, tmp_path, generation=1, hidden=hidden_hashes(hidden))
    tree = _store(trees, {secret: b"secret", "README": b"r"}, links)

    # A box step needs the run's active generation, so ``run`` goes first, on the first loop.
    with pytest.raises(LoopStoppedError, match="symlink at, below or above a hidden path"):
        loop.run(tree, f"cat {secret}")
    assert boxes.opened == 0
    # The loop keeps its first stop and raises it on each later call, so a fresh loop checks each other path.
    reader, _, _ = _loop(owner, engine, step_log, trees, tmp_path, generation=2, hidden=hidden_hashes(hidden))
    with pytest.raises(LoopStoppedError, match="symlink at, below or above a hidden path") as stopped:
        reader.read_file(tree, secret)
    assert "data" not in str(stopped.value)
    assert "eval" not in str(stopped.value)
    lister, _, _ = _loop(owner, engine, step_log, trees, tmp_path, generation=3, hidden=hidden_hashes(hidden))
    with pytest.raises(LoopStoppedError, match="symlink at, below or above a hidden path"):
        lister.list_files(tree)
    fresh, _, _ = _loop(owner, engine, step_log, trees, tmp_path, generation=4, hidden=hidden_hashes(hidden))
    with pytest.raises(LoopStoppedError, match="symlink at, below or above a hidden path"):
        fresh.file_ids(tree)


def test_links_inside_a_hidden_folder_that_no_box_gets_do_not_stop_the_loop(
    owner: tuple[User, int], engine: Engine, step_log: StepLogStore, trees: TreeStore, tmp_path: Path
) -> None:
    """A checked-in venv or ``node_modules/.bin`` below ``eval/`` never reaches a box, so its links are not walked."""
    hidden = hidden_hashes(["eval"])
    loop, _, _ = _loop(owner, engine, step_log, trees, tmp_path, generation=1, hidden=hidden)
    seed = _store(
        trees,
        {"eval/metric.py": b"m", "eval/node_modules/x/cli.js": b"c", "src/a.py": b"a"},
        {
            "eval/.venv/bin/python": "/usr/bin/python3",
            "eval/node_modules/.bin/x": "../x/cli.js",
            "eval/self": "..",
            "eval/up": "../../outside",
        },
    )

    ctx = loop.context(repo_seed=RepoSeed(seed, hidden_hashes=hidden))
    assert ctx.read_file is not None
    assert ctx.read_file(seed, "src/a.py") == b"a"
    assert loop.list_files(seed) == ["src/a.py"]
    listed, _ = loop.run(seed, "find . -type f -o -type l | sort")
    assert listed.stdout.split() == ["./src/a.py"]


def test_links_inside_the_exploit_suite_do_not_stop_the_loop_but_a_link_at_or_into_it_does(
    owner: tuple[User, int], engine: Engine, step_log: StepLogStore, trees: TreeStore, tmp_path: Path
) -> None:
    """The loop always hides ``.optimize/exploits``, so a link in it is dropped; a link that shows it is not."""
    loop, _, _ = _loop(owner, engine, step_log, trees, tmp_path, generation=1)
    tree = _store(
        trees,
        {".optimize/exploits/1/patch.diff": b"secret", "src/a.py": b"a"},
        {".optimize/exploits/1/repo": "../../../src", ".optimize/exploits/python": "/usr/bin/python3"},
    )

    assert loop.read_file(tree, "src/a.py") == b"a"
    assert loop.list_files(tree) == ["src/a.py"]

    for index, links in enumerate([{".optimize/exploits": "../real"}, {".optimize": "real"}]):
        bad, _, _ = _loop(owner, engine, step_log, trees, tmp_path / str(index), generation=1)
        linked = _store(trees, {"real/exploits/1/patch.diff": b"secret", "src/a.py": b"a"}, links)
        with pytest.raises(LoopStoppedError, match="symlink at, below or above a hidden path"):
            bad.read_file(linked, "src/a.py")


def test_a_symlink_loop_stops_the_loop_and_a_link_away_from_hidden_paths_is_kept(
    owner: tuple[User, int], engine: Engine, step_log: StepLogStore, trees: TreeStore, tmp_path: Path
) -> None:
    """A link that names a folder above it cannot be walked to its end; links that show no hidden path are fine."""
    hidden = hidden_hashes(["eval", "data/answer.txt"])
    loop, _, _ = _loop(owner, engine, step_log, trees, tmp_path, generation=1, hidden=hidden)
    kept = _store(
        trees, {"eval/metric.py": b"m", "src/answer.txt": b"a"}, {"docs": "src", "src/again.txt": "../docs/answer.txt"}
    )

    assert loop.list_files(kept) == ["src/answer.txt"]
    result, _ = loop.run(kept, "cat docs/answer.txt")
    assert result.stdout == "a"

    looped = _store(trees, {"eval/metric.py": b"m", "src/a.py": b"a"}, {"src/self": ".."})
    with pytest.raises(LoopStoppedError, match="symlink loop"):
        loop.read_file(looped, "src/a.py")


def test_put_files_cannot_write_a_hidden_path(
    owner: tuple[User, int], engine: Engine, step_log: StepLogStore, trees: TreeStore, tmp_path: Path
) -> None:
    """A tree id is a content hash, so a write to a hidden path would confirm a guess of the hidden content."""
    loop, _, _ = _loop(owner, engine, step_log, trees, tmp_path, generation=1, hidden=hidden_hashes(["eval", "metric"]))
    seed = _store(trees, {"eval/answer.txt": b"42", "metric": b"m", "README": b"r"})

    for guess in (b"42", b"41"):
        with pytest.raises(ValueError, match=r"^path eval/answer.txt may not be changed$"):
            loop.put_files({"eval/answer.txt": guess}, seed)
    with pytest.raises(ValueError, match="may not be changed"):
        loop.put_files({"./eval/answer.txt": b"42"}, seed)
    # A nested write would turn the hidden file into a folder.
    with pytest.raises(ValueError, match=r"^path metric/x may not be changed$"):
        loop.put_files({"metric/x": b"x"}, seed)


def test_put_files_cannot_remove_a_hidden_path_by_writing_a_file_over_its_folder(
    owner: tuple[User, int], engine: Engine, step_log: StepLogStore, trees: TreeStore, tmp_path: Path
) -> None:
    """A file over a folder that holds a hidden path is refused with the same message as for any other path."""
    loop, _, _ = _loop(owner, engine, step_log, trees, tmp_path, generation=1, hidden=hidden_hashes(["eval/metric.py"]))
    seed = _store(trees, {"eval/metric.py": b"m", "eval/notes.md": b"n", "src/a.py": b"a"})

    with pytest.raises(ValueError, match=r"^path eval may not be changed$"):
        loop.put_files({"eval": b"x"}, seed)
    assert loop.read_file(loop.put_files({"src": b"x"}, seed), "src") == b"x"


def test_read_file_and_list_files_hide_the_exploit_suite_as_the_candidate_box_does(
    owner: tuple[User, int], engine: Engine, step_log: StepLogStore, trees: TreeStore, tmp_path: Path
) -> None:
    """The native runtime hides ``.optimize/exploits`` from the proposer, also when the run's hashes leave it out."""
    loop, _, _ = _loop(owner, engine, step_log, trees, tmp_path, generation=1)
    tree = _store(trees, {".optimize/exploits/1/patch.diff": b"secret", ".optimize/notes.md": b"n", "src/a.py": b"a"})

    assert loop.list_files(tree) == [".optimize/notes.md", "src/a.py"]
    assert loop.list_files(tree, ".optimize") == [".optimize/notes.md"]
    assert loop.list_files(tree, ".optimize/exploits") is None
    assert loop.read_file(tree, ".optimize/exploits/1/patch.diff") is None
    listed, _ = loop.run(tree, "find . -type f | sort")
    assert listed.stdout.split() == ["./.optimize/notes.md", "./src/a.py"]
    with pytest.raises(ValueError, match=r"^path \.optimize/exploits/1/patch\.diff may not be changed$"):
        loop.put_files({".optimize/exploits/1/patch.diff": b"secret"}, tree)
    with pytest.raises(ValueError, match=r"^path \.optimize may not be changed$"):
        loop.put_files({".optimize": b"x"}, tree)


@pytest.mark.parametrize(
    "target",
    ["/etc", "/workspace/src", "../out", "../loop-1/src", "src/../../x"],
    ids=["absolute", "absolute-mount", "leaves", "leaves-and-returns", "leaves-from-a-folder"],
)
def test_a_symlink_that_names_an_absolute_path_or_leaves_the_tree_stops_the_loop(
    owner: tuple[User, int], engine: Engine, step_log: StepLogStore, trees: TreeStore, tmp_path: Path, target: str
) -> None:
    """The loop does not know where a box puts the tree, so it cannot check what such a link reaches.

    Args:
        target: The symlink's target.
    """
    loop, _, boxes = _loop(owner, engine, step_log, trees, tmp_path, generation=1, hidden=hidden_hashes(["eval"]))
    tree = _store(trees, {"eval/metric.py": b"m", "src/a.py": b"a"}, {"door": target})

    with pytest.raises(LoopStoppedError, match="names an absolute path or leaves the tree") as stopped:
        loop.list_files(tree)
    assert target not in str(stopped.value)
    assert "door" not in str(stopped.value)
    with pytest.raises(LoopStoppedError):
        loop.run(tree, "cat src/a.py")
    assert boxes.opened == 0


def test_a_bad_seed_stops_the_loop_in_context_and_a_good_seed_is_checked_once(
    owner: tuple[User, int],
    engine: Engine,
    step_log: StepLogStore,
    trees: TreeStore,
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """A seed with a link to a hidden path stops the loop before the engine starts; a checked tree is not walked
    again."""
    hidden = hidden_hashes(["eval"])
    good, _, _ = _loop(owner, engine, step_log, trees, tmp_path / "good", generation=1, hidden=hidden)
    seed = _store(trees, {"src/a.py": b"a"}, {"docs": "src"})
    walks: list[int] = []
    problem = good._link_problem

    def counted(folders: Any) -> str | None:
        """Count one walk of a tree through its links.

        Args:
            folders: The tree's folders.

        Returns:
            What the loop's own walk returns.
        """
        walks.append(len(folders))
        return problem(folders)

    monkeypatch.setattr(good, "_link_problem", counted)
    ctx = good.context(repo_seed=RepoSeed(seed, hidden_hashes=hidden))
    assert ctx.read_file is not None
    assert ctx.read_file(seed, "docs/a.py") is None
    assert ctx.read_file(seed, "src/a.py") == b"a"
    assert len(walks) == 1

    bad, _, boxes = _loop(owner, engine, step_log, trees, tmp_path / "bad", generation=1, hidden=hidden)
    bad_seed = _store(trees, {"real/answer.txt": b"secret"}, {"eval": "real"})
    with pytest.raises(LoopStoppedError, match="symlink at, below or above a hidden path"):
        bad.context(repo_seed=RepoSeed(bad_seed, hidden_hashes=hidden))
    with pytest.raises(LoopStoppedError, match="symlink at, below or above a hidden path"):
        bad.model("hi")
    assert boxes.opened == 0


def test_links_that_a_command_makes_in_a_tree_with_no_hidden_path_do_not_stop_the_loop(
    owner: tuple[User, int], engine: Engine, step_log: StepLogStore, trees: TreeStore, tmp_path: Path
) -> None:
    """``python -m venv`` writes an absolute link; a run output holds no hidden path, so such links are kept."""
    hidden = hidden_hashes(["eval"])
    loop, _, _ = _loop(owner, engine, step_log, trees, tmp_path, generation=1, hidden=hidden)
    seed = _store(trees, {"eval/metric.py": b"m", "src/a.py": b"a"})

    made, after = loop.run(seed, "ln -s /usr/bin/env src/python && ln -s ../../outside src/up && ln -s src/up loop")
    assert made.exit_code == 0
    again, _ = loop.run(after, "cat src/a.py && readlink src/python src/up loop")

    assert again.stdout.split() == ["a/usr/bin/env", "../../outside", "src/up"]
    assert loop.read_file(after, "src/a.py") == b"a"
    assert loop.list_files(after, "src") == ["src/a.py"]


def test_a_command_that_writes_a_hidden_path_fails_as_a_box_step_and_a_resume_replays_it(
    owner: tuple[User, int], engine: Engine, step_log: StepLogStore, trees: TreeStore, tmp_path: Path
) -> None:
    """A tree id is a content hash, so a run output with a hidden path would confirm a guess of hidden content."""
    command = "mkdir -p .optimize/exploits && echo x > .optimize/exploits/a"
    seed = _store(trees, {"src/a.py": b"a"})
    first, _, _ = _loop(owner, engine, step_log, trees, tmp_path, generation=1)
    with pytest.raises(TreeRejectedError) as live_error:
        first.run(seed, command)
    assert ".optimize" not in str(live_error.value)
    result, after = first.run(seed, "cat src/a.py")
    assert result.stdout == "a"
    assert first.list_files(after) == ["src/a.py"]
    assert [kind for _, kind, _ in _steps(engine)] == ["command", "command"]

    _new_generation(engine)
    loop, _, boxes = _loop(owner, engine, step_log, trees, tmp_path, generation=2)
    with pytest.raises(TreeRejectedError) as replayed_error:
        loop.run(seed, command)
    assert str(live_error.value) == str(replayed_error.value)
    assert loop.run(seed, "cat src/a.py") == (result, after)
    assert boxes.opened == 0


def test_a_seed_link_loop_that_reaches_a_hidden_path_stops_the_loop(
    owner: tuple[User, int], engine: Engine, step_log: StepLogStore, trees: TreeStore, tmp_path: Path
) -> None:
    """``a -> .`` reaches the hidden ``a/secret.txt``; the walk cannot follow the loop, so the tree stops the loop."""
    loop, _, boxes = _loop(
        owner, engine, step_log, trees, tmp_path, generation=1, hidden=hidden_hashes(["a/secret.txt"])
    )
    tree = _store(trees, {"secret.txt": b"TOPSECRET", "src/x.py": b"x"}, {"a": "."})

    with pytest.raises(LoopStoppedError, match="symlink loop"):
        loop.run(tree, "cat a/secret.txt")
    assert boxes.opened == 0
    with pytest.raises(LoopStoppedError, match="symlink loop"):
        loop.model("hi")


def test_a_seed_link_that_leaves_the_tree_stops_the_loop_when_the_run_hides_a_path(
    owner: tuple[User, int], engine: Engine, step_log: StepLogStore, trees: TreeStore, tmp_path: Path
) -> None:
    """``eval -> /abs/real`` can reach the hidden ``eval/answer.txt`` in a box, also in a tree with no hidden path."""
    hidden = hidden_hashes(["eval/answer.txt"])
    loop, _, _ = _loop(owner, engine, step_log, trees, tmp_path, generation=1, hidden=hidden)
    tree = _store(trees, {"real/answer.txt": b"TOPSECRET"}, {"eval": "/abs/real"})

    with pytest.raises(LoopStoppedError, match="names an absolute path or leaves the tree"):
        loop.list_files(tree)

    bad, _, _ = _loop(owner, engine, step_log, trees, tmp_path / "bad", generation=1, hidden=hidden)
    with pytest.raises(LoopStoppedError, match="names an absolute path or leaves the tree"):
        bad.context(repo_seed=RepoSeed(tree, hidden_hashes=hidden))


def test_put_files_over_a_seed_keeps_the_strict_link_rule(
    owner: tuple[User, int], engine: Engine, step_log: StepLogStore, trees: TreeStore, tmp_path: Path
) -> None:
    """A tree that put_files writes over the seed can still hold the seed's content, so its links are walked."""
    loop, _, _ = _loop(owner, engine, step_log, trees, tmp_path, generation=1, hidden=hidden_hashes(["a/secret.txt"]))
    seed = _store(trees, {"secret.txt": b"TOPSECRET"}, {"a": "."})
    changed = loop.put_files({"src/x.py": b"x"}, seed)

    with pytest.raises(LoopStoppedError, match="symlink loop"):
        loop.read_file(changed, "src/x.py")


def test_with_only_the_exploit_suite_hidden_a_seed_link_loop_is_kept(
    owner: tuple[User, int], engine: Engine, step_log: StepLogStore, trees: TreeStore, tmp_path: Path
) -> None:
    """The exploit suite alone does not make the walk strict, so repositories with link loops still run."""
    loop, _, _ = _loop(owner, engine, step_log, trees, tmp_path, generation=1)
    tree = _store(trees, {"src/a.py": b"a"}, {"src/self": "..", "venv/python": "/usr/bin/python3"})

    assert loop.read_file(tree, "src/a.py") == b"a"


def test_a_link_with_a_hidden_name_that_a_command_makes_does_not_stop_the_loop_or_its_resume(
    owner: tuple[User, int], engine: Engine, step_log: StepLogStore, trees: TreeStore, tmp_path: Path
) -> None:
    """A run output holds no hidden content, so a link at a hidden name shows only what the box wrote."""
    hidden = hidden_hashes(["eval/answer.txt"])
    seed = _store(trees, {"src/a.py": b"a"})
    command = "mkdir real && echo hi > real/answer.txt && ln -s real eval"

    def drive(loop: EngineLoop) -> tuple[Any, ...]:
        """Make the link, then read, list, run and call the model over the tree after it.

        Args:
            loop: The engine loop.

        Returns:
            What each call gave.
        """
        made, after = loop.run(seed, command)
        changed = loop.put_files({"src/b.py": b"b"}, after)
        again, _ = loop.run(changed, "cat eval/answer.txt")
        return (
            made.exit_code,
            loop.read_file(after, "src/a.py"),
            loop.list_files(changed),
            again.stdout,
            loop.model("hi"),
        )

    first, _, _ = _loop(owner, engine, step_log, trees, tmp_path, generation=1, hidden=hidden)
    live = drive(first)
    assert live[:4] == (0, b"a", ["real/answer.txt", "src/a.py", "src/b.py"], "hi\n")

    _new_generation(engine)
    loop, model, boxes = _loop(owner, engine, step_log, trees, tmp_path, generation=2, hidden=hidden)
    assert drive(loop) == live
    assert boxes.opened == 0
    assert model.prompts == []


def test_a_run_output_with_more_linked_paths_than_the_limit_does_not_stop_the_loop(
    owner: tuple[User, int],
    engine: Engine,
    step_log: StepLogStore,
    trees: TreeStore,
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """A run output holds no hidden content, so many links (a pnpm node_modules) are kept; a seed keeps the limit."""
    monkeypatch.setattr(engine_loop, "_MAX_LINKED_PATHS", 3)
    hidden = hidden_hashes(["eval"])
    loop, _, _ = _loop(owner, engine, step_log, trees, tmp_path, generation=1, hidden=hidden)
    seed = _store(trees, {"src/a.py": b"a"})

    made, after = loop.run(seed, "for n in 1 2 3 4 5; do ln -s src link$n; done")
    assert made.exit_code == 0
    assert loop.read_file(after, "src/a.py") == b"a"

    many = _store(trees, {"src/a.py": b"a"}, {f"link{n}": "src" for n in range(5)})
    with pytest.raises(LoopStoppedError, match="reaches more than 3 paths"):
        loop.read_file(many, "src/a.py")


@pytest.mark.parametrize(
    ("hidden", "files", "refused"),
    [
        (None, {"src/a.py": b"a"}, False),
        (["eval"], {"src/a.py": b"a"}, True),
        (None, {"src/a.py": b"a", ".optimize/exploits/1/patch.diff": b"secret"}, True),
    ],
    ids=["no-hidden-path", "strict", "holds-exploits"],
)
def test_more_linked_paths_than_the_limit_refuse_a_tree_only_in_strict_mode_or_with_a_hidden_path(
    hidden: list[str] | None,
    files: dict[str, bytes],
    refused: bool,
    owner: tuple[User, int],
    engine: Engine,
    step_log: StepLogStore,
    trees: TreeStore,
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """The limit is like a link loop: a run that hides no path of its own refuses only a tree with a hidden path.

    Args:
        hidden: The run's hidden paths, or ``None``.
        files: The tree's files by path.
        refused: Whether the loop refuses the tree.
    """
    monkeypatch.setattr(engine_loop, "_MAX_LINKED_PATHS", 3)
    loop, _, _ = _loop(
        owner, engine, step_log, trees, tmp_path, generation=1, hidden=None if hidden is None else hidden_hashes(hidden)
    )
    many = _store(trees, files, {f"link{n}": "src" for n in range(5)})
    if refused:
        with pytest.raises(LoopStoppedError, match="reaches more than 3 paths"):
            loop.read_file(many, "src/a.py")
    else:
        assert loop.read_file(many, "src/a.py") == b"a"


@pytest.mark.parametrize("stop", ["fence", "close"])
def test_tree_calls_after_a_stop_raise_the_kept_stop_and_touch_no_tree(
    stop: str,
    owner: tuple[User, int],
    engine: Engine,
    step_log: StepLogStore,
    trees: TreeStore,
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """After a fence loss or a close, put_files, read_file and list_files raise the stop and read or write nothing.

    Args:
        stop: How the loop stops: ``fence`` or ``close``.
    """
    seed_tree = _store(trees, {"src/a.py": b"a = 1\n"})
    loop, _, _ = _loop(owner, engine, step_log, trees, tmp_path, generation=1)
    assert loop.read_file(seed_tree, "src/a.py") == b"a = 1\n"
    if stop == "fence":
        _new_generation(engine)
        with pytest.raises(LoopStoppedError):
            loop.model("improve seed")
    else:
        loop.close()
    touched: list[str] = []

    def record(name: str) -> Any:
        """Return a tree-store method that records its use and fails.

        Args:
            name: The method name.

        Returns:
            The fake method.
        """

        def fake(*args: Any, **kwargs: Any) -> Any:
            """Record the use, and fail.

            Args:
                *args: Not used.
                **kwargs: Not used.

            Raises:
                AssertionError: Always.
            """
            touched.append(name)
            raise AssertionError(f"{name} ran after the stop")

        return fake

    for name in ("put_tree", "put_blob", "get_tree", "get_blob"):
        monkeypatch.setattr(trees, name, record(name))

    for call in (
        lambda: loop.put_files({"b.py": b"b"}, seed_tree),
        lambda: loop.read_file(seed_tree, "src/a.py"),
        lambda: loop.list_files(seed_tree, "src"),
    ):
        with pytest.raises(LoopStoppedError):
            call()
    assert touched == []


@pytest.mark.parametrize("stop", ["close", "divergence"])
def test_a_context_after_a_stop_raises_the_kept_stop_and_writes_no_tree(
    stop: str,
    owner: tuple[User, int],
    engine: Engine,
    step_log: StepLogStore,
    trees: TreeStore,
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """A context after a close or a divergence raises the kept stop, with its type, and writes no visible seed.

    Args:
        stop: How the loop stops: ``close`` or ``divergence``.
    """
    hidden = hidden_hashes(["metric.py"])
    raw = _store(trees, {"README": b"seed", "metric.py": b"secret"})
    if stop == "close":
        loop, _, _ = _loop(owner, engine, step_log, trees, tmp_path, generation=1, hidden=hidden)
        loop.close()
        kept: type[LoopStoppedError] = LoopStoppedError
    else:
        first, _, _ = _loop(owner, engine, step_log, trees, tmp_path, generation=1, hidden=hidden)
        assert first.model("improve seed") == "seedx"
        _new_generation(engine)
        loop, _, _ = _loop(owner, engine, step_log, trees, tmp_path, generation=2, hidden=hidden)
        with pytest.raises(StepDivergedError):
            loop.model("improve other")
        kept = StepDivergedError
    writes: list[str] = []
    for name in ("put_tree", "put_blob"):
        real = getattr(trees, name)

        def spy(*args: Any, _name: str = name, _real: Any = real, **kwargs: Any) -> Any:
            """Record a tree-store write, then make it.

            Args:
                *args: The write's arguments.
                _name: The method name.
                _real: The real method.
                **kwargs: The write's keyword arguments.

            Returns:
                What the real write returns.
            """
            writes.append(_name)
            return _real(*args, **kwargs)

        monkeypatch.setattr(trees, name, spy)

    for call in (lambda: loop.context(repo_seed=RepoSeed(raw, hidden_hashes=hidden)), loop.context):
        with pytest.raises(LoopStoppedError) as stopped:
            call()
        assert type(stopped.value) is kept
    assert writes == []


def test_a_prompt_that_is_no_json_is_a_kept_stop_and_a_stop_comes_before_the_hash(
    owner: tuple[User, int], engine: Engine, step_log: StepLogStore, trees: TreeStore, tmp_path: Path
) -> None:
    """A prompt that does not hash stops the loop with no step, and after a stop the kept stop comes first."""
    loop, model, _ = _loop(owner, engine, step_log, trees, tmp_path, generation=1)
    with pytest.raises(LoopStoppedError, match="cannot be hashed"):
        loop.call_model([{"role": "user", "content": object()}])
    with pytest.raises(LoopStoppedError, match="cannot be hashed"):
        loop.model("improve seed")
    assert model.prompts == []
    assert _steps(engine) == []

    closed, _, _ = _loop(owner, engine, step_log, trees, tmp_path, generation=1)
    closed.close()
    with pytest.raises(LoopStoppedError, match="is closed"):
        closed.call_model([{"role": "user", "content": object()}])


def test_grants_that_fail_to_resolve_are_a_kept_stop_and_a_stop_comes_before_them(
    owner: tuple[User, int], engine: Engine, step_log: StepLogStore, trees: TreeStore, tmp_path: Path
) -> None:
    """Grants of the wrong type stop the loop with no box, and after a stop the kept stop comes first."""
    seed_tree = _tree(trees, "README", "seed")
    loop, _, boxes = _loop(owner, engine, step_log, trees, tmp_path, generation=1)
    with pytest.raises(LoopStoppedError, match="grants do not resolve"):
        loop.run(seed_tree, "true", (object(),))  # type: ignore[arg-type]
    with pytest.raises(LoopStoppedError, match="grants do not resolve"):
        loop.run(seed_tree, "true")
    assert boxes.opened == 0

    closed, _, _ = _loop(owner, engine, step_log, trees, tmp_path, generation=1)
    closed.close()
    with pytest.raises(LoopStoppedError, match="is closed"):
        closed.run(seed_tree, "true", (object(),))  # type: ignore[arg-type]


def test_a_replayed_record_with_an_unknown_error_diverges_and_stays_in_the_replay(
    owner: tuple[User, int],
    engine: Engine,
    step_log: StepLogStore,
    trees: TreeStore,
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """A box record whose error the loop cannot raise again is a divergence, and the replay does not pass it."""
    outputs, metric = _tree(trees, "answer.txt", "a"), _tree(trees, "metric.txt", "len")
    first, _, _ = _loop(owner, engine, step_log, trees, tmp_path, generation=1)
    with pytest.raises(MetricFailedError):
        first.score(outputs, metric, "exit 3")

    _new_generation(engine)
    monkeypatch.setattr(
        engine_loop,
        "_BOX_ERRORS",
        {name: error for name, error in engine_loop._BOX_ERRORS.items() if name != "MetricFailedError"},
    )
    loop, _, boxes = _loop(owner, engine, step_log, trees, tmp_path, generation=2)
    with pytest.raises(StepDivergedError, match="unknown error 'MetricFailedError'"):
        loop.score(outputs, metric, "exit 3")
    assert len(loop._logs[ROOT_AGENT_ID].replay) == 1
    with pytest.raises(LoopStoppedError):
        loop.score(outputs, metric, "exit 3")
    assert boxes.opened == 0


def test_a_metric_source_that_is_not_a_fresh_box_source_is_refused(
    owner: tuple[User, int], engine: Engine, step_log: StepLogStore, trees: TreeStore, tmp_path: Path
) -> None:
    """Each score must run in its own new box, so the loop takes only a source that opens one for each call."""
    loop, model, boxes = _loop(owner, engine, step_log, trees, tmp_path, generation=1)
    with pytest.raises(TypeError, match="RuntimeBoxSource"):
        EngineLoop(
            loop._run,
            engine=engine,
            trees=trees,
            candidates=_CountingSource(boxes),
            metrics=object(),  # type: ignore[arg-type]
            model=model,
            model_max_cents=2,
            run_dir=tmp_path / "other",
        )


def _cancel(engine: Engine) -> None:
    """Cancel the job at its generation, so the owner check fails while the budget still admits paid work.

    Args:
        engine: The database.
    """
    with Session(engine) as session:
        session.execute(update(JobModel).where(JobModel.optimization_id == RUN_ID).values(status="cancelled"))
        session.commit()


def test_a_failed_owner_check_before_a_model_call_is_a_kept_stop_with_no_call_and_no_step(
    owner: tuple[User, int], engine: Engine, step_log: StepLogStore, trees: TreeStore, tmp_path: Path
) -> None:
    """Another writer owns the run: the model call stops at the owner check, before the provider and the commit."""
    loop, model, _ = _loop(owner, engine, step_log, trees, tmp_path, generation=1)
    assert loop.model("improve seed") == "seedx"
    _cancel(engine)

    for _ in range(2):
        with pytest.raises(LoopStoppedError, match="no longer active") as stopped:
            loop.model("improve seedx")
        assert type(stopped.value) is LoopStoppedError
    assert model.prompts == ["improve seed"]
    assert len(_steps(engine)) == 1


def test_a_failed_owner_check_before_a_room_read_is_a_kept_stop_with_no_step(
    owner: tuple[User, int], engine: Engine, step_log: StepLogStore, trees: TreeStore, tmp_path: Path
) -> None:
    """Another writer owns the run: the room read commits no step, and the agent's next seq does not move."""
    loop, _, _ = _loop(owner, engine, step_log, trees, tmp_path, generation=1)
    assert loop.model("improve seed") == "seedx"
    _cancel(engine)

    for _ in range(2):
        with pytest.raises(LoopStoppedError, match="no longer active") as stopped:
            loop.remaining_cost_usd()
        assert type(stopped.value) is LoopStoppedError
    assert len(_steps(engine)) == 1
    assert loop._logs[ROOT_AGENT_ID].agent.next_seq == 1


def test_an_unpaid_step_past_the_last_seq_is_refused_before_its_work(
    owner: tuple[User, int],
    engine: Engine,
    step_log: StepLogStore,
    trees: TreeStore,
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """A room read and a box failure record past ``SEQ_MAX`` stop the loop before they read or write anything."""
    loop, _, _ = _loop(owner, engine, step_log, trees, tmp_path, generation=1)
    assert loop.remaining_cost_usd() == pytest.approx(10.0)
    agent = loop._logs[ROOT_AGENT_ID].agent
    agent.next_seq = SEQ_MAX + 1
    writes: list[Any] = []
    monkeypatch.setattr(trees, "put_tree", lambda *args: writes.append(args))

    with pytest.raises(LoopStoppedError, match="next_seq is not an int"):
        loop.remaining_cost_usd()
    assert writes == []
    assert agent.next_seq == SEQ_MAX + 1
    assert len(_steps(engine)) == 1


def test_a_box_failure_record_past_the_last_seq_is_refused_by_the_agent(
    owner: tuple[User, int], engine: Engine, step_log: StepLogStore, trees: TreeStore, tmp_path: Path
) -> None:
    """The box failure record goes through the agent's seq check, so it commits nothing past ``SEQ_MAX``."""
    seed = _tree(trees, "README", "seed")
    loop, _, _ = _loop(owner, engine, step_log, trees, tmp_path, generation=1)
    loop.run(seed, "true")
    agent = loop._logs[ROOT_AGENT_ID].agent
    agent.next_seq = SEQ_MAX + 1

    with pytest.raises(LoopStoppedError, match="next_seq is not an int"):
        loop.run(seed, "true", (Grant("api.example.com", credential=True),))
    assert agent.next_seq == SEQ_MAX + 1
    assert len(_steps(engine)) == 1


def test_a_model_failure_with_an_unknown_bill_settles_nothing_on_the_lease(
    owner: tuple[User, int], engine: Engine, step_log: StepLogStore, trees: TreeStore, tmp_path: Path
) -> None:
    """A failed call whose bill the client cannot know costs 0: Skynet takes the loss (Gilad, 2026-11-17)."""
    loop, model, _ = _loop(owner, engine, step_log, trees, tmp_path, generation=1)
    model.error = TimeoutError("the provider did not answer")

    with pytest.raises(ModelCallFailedError, match="TimeoutError"):
        loop.model("improve seed")
    failed = step_log.get_step(step_id_for(RUN_ID, ROOT_AGENT_ID, 0))
    assert failed["body"]["error"] == "TimeoutError"
    assert Decimal(failed["body"]["cost_cents"]) == 0
    assert loop.remaining_cost_usd() == pytest.approx(10.0)


def test_a_billed_model_failure_settles_its_known_cost_and_records_it_in_the_step(
    owner: tuple[User, int], engine: Engine, step_log: StepLogStore, trees: TreeStore, tmp_path: Path
) -> None:
    """A failure that the provider billed is no free step: the ledger and the step both hold its cost."""
    loop, model, _ = _loop(owner, engine, step_log, trees, tmp_path, generation=1)
    model.error = ModelCallBilledError("1.5")

    with pytest.raises(ModelCallFailedError, match="ModelCallBilledError"):
        loop.model("improve seed")
    failed = step_log.get_step(step_id_for(RUN_ID, ROOT_AGENT_ID, 0))
    assert failed["body"]["error"] == "ModelCallBilledError"
    assert Decimal(failed["body"]["cost_cents"]) == Decimal("1.5")
    assert loop.remaining_cost_usd() == pytest.approx(9.985)


@pytest.mark.parametrize(("billed", "committed"), [("1.5", "1.5"), ("0.0000000001", "0.000000001")])
def test_a_billed_model_failure_gives_the_engine_its_committed_cost_live_and_on_a_replay(
    owner: tuple[User, int],
    engine: Engine,
    step_log: StepLogStore,
    trees: TreeStore,
    tmp_path: Path,
    billed: str,
    committed: str,
) -> None:
    """The engine's failure carries the cost that the ledger committed, so its own cost count holds it.

    Args:
        owner: Alice and her budget generation.
        engine: Engine with the step log and job tables.
        step_log: The run's step log.
        trees: The step log's tree store.
        tmp_path: The loop's local folder.
        billed: What the provider billed, in cents.
        committed: The cost that the ledger commits, rounded up to nine fractional digits.
    """
    first, model, _ = _loop(owner, engine, step_log, trees, tmp_path, generation=1)
    model.error = ModelCallBilledError(billed)
    with pytest.raises(ModelCallFailedError) as live:
        first.model("improve seed")
    assert live.value.cost_cents == Decimal(committed)

    _new_generation(engine)
    resumed, resumed_model, _ = _loop(owner, engine, step_log, trees, tmp_path, generation=2)
    with pytest.raises(ModelCallFailedError) as replayed:
        resumed.model("improve seed")
    assert replayed.value.cost_cents == Decimal(committed)
    assert str(replayed.value) == str(live.value)
    assert resumed_model.prompts == []


def test_an_unbilled_model_failure_gives_the_engine_no_cost_live_and_on_a_replay(
    owner: tuple[User, int], engine: Engine, step_log: StepLogStore, trees: TreeStore, tmp_path: Path
) -> None:
    """A failed call with no known bill gives the engine a cost of 0, live and on a replay.

    Args:
        owner: Alice and her budget generation.
        engine: Engine with the step log and job tables.
        step_log: The run's step log.
        trees: The step log's tree store.
        tmp_path: The loop's local folder.
    """
    first, model, _ = _loop(owner, engine, step_log, trees, tmp_path, generation=1)
    model.error = TimeoutError("the provider did not answer")
    with pytest.raises(ModelCallFailedError) as live:
        first.model("improve seed")
    assert live.value.cost_cents == 0

    _new_generation(engine)
    resumed, _, _ = _loop(owner, engine, step_log, trees, tmp_path, generation=2)
    with pytest.raises(ModelCallFailedError) as replayed:
        resumed.model("improve seed")
    assert replayed.value.cost_cents == 0


def test_an_engine_that_ends_before_its_replay_is_over_diverges_at_finish(
    owner: tuple[User, int], engine: Engine, step_log: StepLogStore, trees: TreeStore, tmp_path: Path
) -> None:
    """A resume that skips committed steps, in an open agent or an agent that never opens, is a kept divergence."""
    first, _, _ = _loop(owner, engine, step_log, trees, tmp_path, generation=1)
    first.model("improve seed")
    first.model("improve seedx")
    with usage_scope(**{LANE_KEY: "lane-a"}):
        first.model("improve lane")
    first.finish()

    _new_generation(engine)
    loop, model, _ = _loop(owner, engine, step_log, trees, tmp_path, generation=2)
    assert loop.model("improve seed") == "seedx"
    with pytest.raises(StepDivergedError) as diverged:
        loop.finish()
    assert f"'{ROOT_AGENT_ID}/lane-a'" in str(diverged.value)
    assert f"'{ROOT_AGENT_ID}'" in str(diverged.value)
    with pytest.raises(StepDivergedError):
        loop.model("improve seedx")
    assert model.prompts == []


def test_finish_does_not_count_the_runs_generation_commits(
    owner: tuple[User, int], engine: Engine, step_log: StepLogStore, trees: TreeStore, tmp_path: Path
) -> None:
    """The run's own generation commits are no agent of the loop, so a run that ends a generation still finishes."""
    loop, _, _ = _loop(owner, engine, step_log, trees, tmp_path, generation=1)
    loop.model("improve seed")
    step_id = loop._run.end_generation(["seedx"], "seedx")
    step_log.durable(step_id, 1)

    loop.finish()
    assert loop.model("improve seedx") == "seedxx"


def test_finish_does_not_count_a_harness_capture_of_the_run(
    owner: tuple[User, int], engine: Engine, step_log: StepLogStore, trees: TreeStore, tmp_path: Path
) -> None:
    """A harness capture commits to the run's step log under its own agent, which the loop does not replay."""
    first, _, _ = _loop(owner, engine, step_log, trees, tmp_path, generation=1)
    first.model("improve seed")
    capture = step_log.commit(
        run_id=RUN_ID,
        agent_id="harness",
        seq=0,
        kind="command",
        execution_generation=1,
        grant_list_id="grants-1",
        body={"source": "exec", "command": "true"},
        tree_id=trees.put_tree(RUN_ID, []),
    )
    step_log.durable(capture, 1)
    first.finish()

    _new_generation(engine)
    loop, model, _ = _loop(owner, engine, step_log, trees, tmp_path, generation=2)
    assert loop.model("improve seed") == "seedx"
    loop.finish()
    assert model.prompts == []


@pytest.mark.parametrize(
    "path", ["eval/secret/../x", "a/./b", "a//b", "a/b\0c", "a/", ".git/config", "a/.GIT/x", "a/.Git"]
)
def test_put_files_refuses_a_bad_path_part_before_it_writes_any_object(
    path: str,
    owner: tuple[User, int],
    engine: Engine,
    step_log: StepLogStore,
    trees: TreeStore,
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """The tree store refuses a bad name only at its folder, so put_files checks each part before the first blob.

    Args:
        path: A path with an empty, ``.`` or ``..`` part, a ``.git`` part in any letter case, or a NUL byte.
    """
    loop, _, _ = _loop(owner, engine, step_log, trees, tmp_path, generation=1)
    writes: list[Any] = []
    monkeypatch.setattr(step_log, "put_object", lambda *args: writes.append(args))

    with pytest.raises(ValueError, match="bad tree entry name"):
        loop.put_files({"ok.txt": b"1", path: b"2"})
    assert writes == []


def _put_raw_tree(trees: TreeStore, rows: list[list[str]]) -> str:
    """Store a tree object without :func:`encode_tree`, as a tree stored before SKY-810 can be.

    Args:
        trees: The tree store.
        rows: The ``[name, mode, object_id]`` entries, unchecked.

    Returns:
        The tree's object id.
    """
    stored = b"tree\0" + json.dumps(rows, separators=(",", ":")).encode("utf-8")
    object_id = object_id_of(stored)
    trees._step_log.put_object(RUN_ID, object_id, stored)
    return object_id


def test_a_tree_stored_before_sky_810_loses_its_git_entries_on_a_rewrite(
    owner: tuple[User, int], engine: Engine, step_log: StepLogStore, trees: TreeStore, tmp_path: Path
) -> None:
    """The context and put_files write back a legacy tree without its ``.git`` entries, as other writers do."""
    hidden = hidden_hashes(["metric.py"])
    loop, _, _ = _loop(owner, engine, step_log, trees, tmp_path, generation=1, hidden=hidden)
    blob = trees.put_blob(RUN_ID, b"seed")
    secret = trees.put_blob(RUN_ID, b"secret")
    git = _put_raw_tree(trees, [["config", "file", blob]])
    sub = _put_raw_tree(trees, [[".GIT", "tree", git], ["a.py", "file", blob]])
    raw = _put_raw_tree(
        trees, [[".GIT", "tree", git], ["README", "file", blob], ["metric.py", "file", secret], ["sub", "tree", sub]]
    )

    ctx = loop.context(repo_seed=RepoSeed(raw, hidden_hashes=hidden))
    assert ctx.repo_seed is not None
    assert ctx.repo_seed.tree_id == _store(trees, {"README": b"seed", "sub/a.py": b"seed"})

    written = loop.put_files({"sub/b.py": b"b"}, raw)
    assert written == _store(trees, {"README": b"seed", "metric.py": b"secret", "sub/a.py": b"seed", "sub/b.py": b"b"})


def test_a_legacy_tree_with_only_git_entries_reads_as_a_tree_without_them(
    owner: tuple[User, int], engine: Engine, step_log: StepLogStore, trees: TreeStore, tmp_path: Path
) -> None:
    """Reads, listings and the context hide ``.git`` entries at any depth, as if the tree never held them.

    The ``empty/`` folder holds only a ``.Git`` file, so it reads as an empty folder, not as a hidden one.
    """
    hidden = hidden_hashes(["metric.py"])
    loop, _, _ = _loop(owner, engine, step_log, trees, tmp_path, generation=1, hidden=hidden)
    blob = trees.put_blob(RUN_ID, b"seed")
    git = _put_raw_tree(trees, [["config", "file", blob], ["x", "file", blob]])
    sub = _put_raw_tree(trees, [[".GIT", "tree", git], ["a.py", "file", blob]])
    empty = _put_raw_tree(trees, [[".Git", "file", blob]])
    legacy = _put_raw_tree(
        trees, [[".GIT", "tree", git], ["README", "file", blob], ["empty", "tree", empty], ["sub", "tree", sub]]
    )
    clean_sub = trees.put_tree(RUN_ID, [TreeEntry("a.py", "file", blob)])
    clean = trees.put_tree(
        RUN_ID,
        [
            TreeEntry("README", "file", blob),
            TreeEntry("empty", "tree", trees.put_tree(RUN_ID, [])),
            TreeEntry("sub", "tree", clean_sub),
        ],
    )

    ctx = loop.context(repo_seed=RepoSeed(legacy, hidden_hashes=hidden))
    assert ctx.repo_seed is not None
    assert ctx.repo_seed.tree_id == clean
    assert loop.list_files(legacy) == ["README", "sub/a.py"]
    for path in ["", "sub", "empty", ".GIT", ".git", "sub/.GIT", "empty/.Git"]:
        assert loop.list_files(legacy, path) == loop.list_files(clean, path)
    for path in ["README", "sub/a.py", "sub/.GIT/x", ".GIT/config", ".git/config", "empty/.Git"]:
        assert loop.read_file(legacy, path) == loop.read_file(clean, path)
    assert loop.read_file(legacy, "sub/.GIT/x") is None


def test_a_legacy_folder_with_only_a_hidden_folder_and_git_lists_as_missing(
    owner: tuple[User, int], engine: Engine, step_log: StepLogStore, trees: TreeStore, tmp_path: Path
) -> None:
    """A folder that holds only a hidden folder and a ``.git`` file lists as missing, as without the ``.git``."""
    hidden = hidden_hashes(["d/eval"])
    loop, _, _ = _loop(owner, engine, step_log, trees, tmp_path, generation=1, hidden=hidden)
    blob = trees.put_blob(RUN_ID, b"seed")
    evals = trees.put_tree(RUN_ID, [TreeEntry("score.py", "file", blob)])
    folder = _put_raw_tree(trees, [[".git", "file", blob], ["eval", "tree", evals]])
    tree = _put_raw_tree(trees, [["README", "file", blob], ["d", "tree", folder]])

    assert loop.list_files(tree, "d") is None


def test_a_symlink_below_a_legacy_git_entry_does_not_stop_the_loop(
    owner: tuple[User, int], engine: Engine, step_log: StepLogStore, trees: TreeStore, tmp_path: Path
) -> None:
    """The link check skips a ``.git`` entry, as the box never gets it, so a link below it stops nothing."""
    hidden = hidden_hashes(["metric.py"])
    loop, _, _ = _loop(owner, engine, step_log, trees, tmp_path, generation=1, hidden=hidden)
    readme = trees.put_blob(RUN_ID, b"x")
    git = _put_raw_tree(trees, [["l", "symlink", trees.put_blob(RUN_ID, b"/etc")]])
    tree = _put_raw_tree(trees, [[".git", "tree", git], ["README", "file", readme]])

    assert loop.read_file(tree, "README") == b"x"


def test_put_files_drops_git_entries_from_folders_it_does_not_change(
    owner: tuple[User, int], engine: Engine, step_log: StepLogStore, trees: TreeStore, tmp_path: Path
) -> None:
    """An unchanged folder of the base loses its ``.git`` entries too, so the id matches a base without them."""
    loop, _, _ = _loop(owner, engine, step_log, trees, tmp_path, generation=1)
    blob = trees.put_blob(RUN_ID, b"seed")
    git = _put_raw_tree(trees, [["config", "file", blob]])
    sub = _put_raw_tree(trees, [[".GIT", "tree", git], ["a.py", "file", blob]])
    root = _put_raw_tree(trees, [["README", "file", blob], ["sub", "tree", sub]])

    clean = _store(trees, {"README": b"seed", "sub/a.py": b"seed"})

    assert loop.put_files({"README": b"y"}, root) == loop.put_files({"README": b"y"}, clean)


def test_put_files_drops_git_entries_two_levels_below_a_folder_it_does_not_change(
    owner: tuple[User, int], engine: Engine, step_log: StepLogStore, trees: TreeStore, tmp_path: Path
) -> None:
    """A ``.git`` entry deep in an unchanged folder goes too, so the id matches a base without it."""
    loop, _, _ = _loop(owner, engine, step_log, trees, tmp_path, generation=1)
    blob = trees.put_blob(RUN_ID, b"seed")
    git = _put_raw_tree(trees, [["config", "file", blob]])
    x = _put_raw_tree(trees, [[".git", "tree", git], ["b.py", "file", blob]])
    sub = _put_raw_tree(trees, [["a.py", "file", blob], ["x", "tree", x]])
    root = _put_raw_tree(trees, [["README", "file", blob], ["sub", "tree", sub]])

    clean = _store(trees, {"README": b"seed", "sub/a.py": b"seed", "sub/x/b.py": b"seed"})

    assert loop.put_files({"README": b"y"}, root) == loop.put_files({"README": b"y"}, clean)


def test_put_files_keeps_hidden_paths_in_a_folder_it_does_not_change(
    owner: tuple[User, int], engine: Engine, step_log: StepLogStore, trees: TreeStore, tmp_path: Path
) -> None:
    """Dropping ``.git`` from an unchanged folder keeps its hidden files, which the proposer never wrote."""
    hidden = hidden_hashes(["eval/x/answer.txt"])
    loop, _, _ = _loop(owner, engine, step_log, trees, tmp_path, generation=1, hidden=hidden)
    blob = trees.put_blob(RUN_ID, b"seed")
    git = _put_raw_tree(trees, [["config", "file", blob]])
    x = _put_raw_tree(trees, [[".git", "tree", git], ["answer.txt", "file", blob]])
    evals = _put_raw_tree(trees, [["score.py", "file", blob], ["x", "tree", x]])
    base = _put_raw_tree(trees, [["README", "file", blob], ["eval", "tree", evals]])

    tree = loop.put_files({"README": b"y"}, base)

    def stored(tree_id: str) -> dict[str, str]:
        """Return the ids of a stored tree's entries by name."""
        return {entry.name: entry.object_id for entry in trees.get_tree(RUN_ID, tree_id)}

    folder = stored(tree)["eval"]
    assert sorted(stored(folder)) == ["score.py", "x"]
    assert sorted(stored(stored(folder)["x"])) == ["answer.txt"]


def test_put_files_walks_an_unchanged_folder_once_for_each_loop(
    owner: tuple[User, int],
    engine: Engine,
    step_log: StepLogStore,
    trees: TreeStore,
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """A second write over the same base does not read its unchanged folder again."""
    loop, _, _ = _loop(owner, engine, step_log, trees, tmp_path, generation=1)
    base = _store(trees, {"README": b"seed", "sub/a.py": b"seed"})
    sub = next(entry.object_id for entry in trees.get_tree(RUN_ID, base) if entry.name == "sub")
    reads: list[str] = []
    stored = loop._stored
    monkeypatch.setattr(loop, "_stored", lambda tree_id: reads.append(tree_id) or stored(tree_id))

    first = loop.put_files({"README": b"y"}, base)
    second = loop.put_files({"README": b"z"}, base)

    assert first != second
    assert reads.count(sub) == 1


class _Loads:
    """Record the blob, object, pack-lookup and range loads of a capped read.

    A test uses the record to show that a large file never loads and that a read finds its object once.
    """

    def __init__(self, monkeypatch: pytest.MonkeyPatch) -> None:
        """Wrap the blob read, the step log's object read and pack lookup, and the object store's range read.

        Args:
            monkeypatch: Pytest's patcher; it undoes the wraps after the test.
        """
        self.blobs: list[str] = []
        self.objects: list[str] = []
        self.locations: list[str] = []
        self.ranges: list[int | None] = []
        get_blob, get_object, get = TreeStore.get_blob, StepLogStore.get_object, LocalObjectStore.get
        locate = StepLogStore._object_location

        def blob(store: TreeStore, run_id: str, object_id: str, max_bytes: int | None = None) -> bytes | None:
            """Record a blob that loads; a refused blob raises before the record.

            Args:
                store: The tree store.
                run_id: The job that reads.
                object_id: The blob's id.
                max_bytes: The cap of the read.

            Returns:
                The blob's bytes.
            """
            data = get_blob(store, run_id, object_id, max_bytes)
            self.blobs.append(object_id)
            return data

        def stored(store: StepLogStore, run_id: str, object_id: str, max_bytes: int | None = None) -> bytes | None:
            """Record an object that loads; a refused object raises before the record.

            Args:
                store: The step log.
                run_id: The job that reads.
                object_id: The object's id.
                max_bytes: The cap of the read.

            Returns:
                The object's stored bytes.
            """
            data = get_object(store, run_id, object_id, max_bytes)
            self.objects.append(object_id)
            return data

        def located(store: StepLogStore, run_id: str, object_id: str) -> tuple[str, int, int] | None:
            """Record a pack lookup.

            Args:
                store: The step log.
                run_id: The job that reads.
                object_id: The object's id.

            Returns:
                The object's pack key, offset and length.
            """
            self.locations.append(object_id)
            return locate(store, run_id, object_id)

        def ranged(store: LocalObjectStore, key: str, offset: int = 0, length: int | None = None) -> bytes:
            """Record the length of a range read.

            Args:
                store: The object store.
                key: The pack key.
                offset: The first byte.
                length: The bytes to read; ``None`` reads to the end.

            Returns:
                The bytes of the range.
            """
            self.ranges.append(length)
            return get(store, key, offset, length)

        monkeypatch.setattr(TreeStore, "get_blob", blob)
        monkeypatch.setattr(StepLogStore, "get_object", stored)
        monkeypatch.setattr(StepLogStore, "_object_location", located)
        monkeypatch.setattr(LocalObjectStore, "get", ranged)


def test_a_capped_read_refuses_a_larger_file_before_it_loads_from_the_buffer_or_a_pack(
    owner: tuple[User, int], engine: Engine, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """A file above the cap never loads, before or after its flush; a resume on another worker gets the same answer.

    Before the flush the file is in the buffer; after it, a second worker at the next generation reads the tree
    from its pack, as a resume does. The step logs have a long window, so only the explicit flush writes. After
    the flush, a capped read that passes the cap finds its object in one pack lookup.

    Args:
        owner: Alice and her budget generation.
        engine: The database.
        tmp_path: Pytest's temporary folder; it holds the shared object store.
        monkeypatch: Pytest's patcher, for the load records.
    """
    big = b"\0" * (1 << 20)
    first_log = StepLogStore(engine, LocalObjectStore(tmp_path / "objects"), "worker-1", flush_window_seconds=60.0)
    second_log = StepLogStore(engine, LocalObjectStore(tmp_path / "objects"), "worker-2", flush_window_seconds=60.0)
    try:
        first_trees = TreeStore(first_log)
        tree = _store(first_trees, {"big.bin": big, "src/a.py": b"abcd"})
        big_id = first_trees.put_blob(RUN_ID, big)
        small_id = first_trees.put_blob(RUN_ID, b"abcd")
        first, _, _ = _loop(owner, engine, first_log, first_trees, tmp_path, generation=1)
        ctx = first.context()
        assert ctx.read_file is not None
        loads = _Loads(monkeypatch)

        with pytest.raises(FileTooLargeError, match=f"^file big.bin has {len(big)} bytes, more than the cap of 4$"):
            ctx.read_file(tree, "big.bin", max_bytes=4)
        assert ctx.read_file(tree, "src/a.py", max_bytes=4) == b"abcd"
        with pytest.raises(FileTooLargeError) as small:
            ctx.read_file(tree, "src/a.py", max_bytes=3)
        assert (small.value.size, small.value.max_bytes) == (4, 3)
        assert big_id not in loads.blobs + loads.objects
        assert loads.ranges == []

        first_log.flush()
        _new_generation(engine)
        resumed, _, _ = _loop(owner, engine, second_log, TreeStore(second_log), tmp_path, generation=2)
        with pytest.raises(FileTooLargeError) as again:
            resumed.read_file(tree, "big.bin", max_bytes=len(big) - 1)
        assert (again.value.size, again.value.max_bytes) == (len(big), len(big) - 1)
        loads.locations.clear()
        assert resumed.read_file(tree, "src/a.py", max_bytes=4) == b"abcd"
        assert loads.locations.count(small_id) == 1
        assert big_id not in loads.blobs + loads.objects
        # The tree reads come from the pack, and none of them is as long as the large file.
        assert loads.ranges
        assert all(length is not None and length < len(big) for length in loads.ranges)
        assert resumed.read_file(tree, "big.bin", max_bytes=len(big)) == big
    finally:
        first_log.close()
        second_log.close()


def test_a_capped_read_gives_none_for_a_hidden_missing_or_folder_path(
    owner: tuple[User, int], engine: Engine, step_log: StepLogStore, trees: TreeStore, tmp_path: Path
) -> None:
    """A capped read keeps the rules of a read: a hidden path gives ``None`` like a missing one, and tells no size.

    Args:
        owner: Alice and her budget generation.
        engine: The database.
        step_log: The step log of the loop.
        trees: The tree store.
        tmp_path: Pytest's temporary folder.
    """
    loop, _, boxes = _loop(owner, engine, step_log, trees, tmp_path, generation=1, hidden=hidden_hashes(["eval"]))
    tree = _store(trees, {"eval/metric.py": b"secret", "src/a.py": b"a", "empty.txt": b""})

    for path in ["eval/metric.py", "eval", "missing.py", "src", "src/a.py/x"]:
        assert loop.read_file(tree, path, max_bytes=0) is None
    assert loop.read_file(tree, "empty.txt", max_bytes=0) == b""
    with pytest.raises(FileTooLargeError):
        loop.read_file(tree, "src/a.py", max_bytes=0)
    assert loop.read_file(tree, "src/a.py") == b"a"
    assert boxes.opened == 0
    assert _steps(engine) == []
