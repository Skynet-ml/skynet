"""Tests for GEPA: parallel batch scoring (SKY-429), and repository runs on the worker engine loop (SKY-720).

The engine-loop tests use a SQLite file, the local-filesystem object store,
local subprocess boxes and a fake model: no network and no paid call.
"""

from __future__ import annotations

import contextvars
import re
import threading
import time
from collections.abc import Iterator
from decimal import Decimal
from pathlib import Path
from typing import Any

import pytest
from sqlalchemy import create_engine, select, update
from sqlalchemy.engine import Engine
from sqlalchemy.orm import Session

from core.billing.budgets import BudgetService
from core.exceptions import ServiceError
from core.models.blackbox import BLACKBOX_HARNESS_CUSTOM, BLACKBOX_TARGET_AGENT, BlackboxTarget
from core.sandbox.errors import SandboxInfraError
from core.sandbox.trees import BoxFailureError, GrantDeniedError, TreeRejectedError
from core.storage.models import AgentStepModel, Base, BillingCustomerModel, JobModel
from core.storage.object_store import LocalObjectStore
from core.storage.step_log_store import StepLogStore
from core.storage.tree_store import TreeEntry, TreeStore
from core.storage.workspace_store import WorkspaceStore
from core.worker.actors import User
from core.worker.engine_loop import ROOT_AGENT_ID, EngineLoop, LoopStoppedError, ModelCallFailedError

from ...budget_stop import BudgetReached
from .. import gepa_engine as gepa_mod
from ..agent_eval import SandboxAgentScorer
from ..gepa_engine import GepaEngine
from ..harness import GatewayConfig
from ..harness_bridge import hidden_hashes
from ..native_runtime import NativeOptions
from ..protocol import BudgetExhaustedError, EvalServer, ModelReply, RepoSeed, Result, Task
from ..sandbox import CommandResult, LocalSubprocessRuntime, SandboxSpec
from ..sandbox_contract import SandboxSession
from ..tree_sandbox import RuntimeBoxSource
from .mocks import FakeSandboxRuntime, FakeSandboxSession, make_ctx, vowel_scorer

_GATEWAY = GatewayConfig(url="https://gw.example/v1", api_key="secret-key")
_ATTEMPT_ONE_KEY = re.compile(r"^agent-run:[0-9a-f]{32}:attempt-1$")
_CALLER_TAG: contextvars.ContextVar[str | None] = contextvars.ContextVar("caller_tag", default=None)


def _agent_target() -> BlackboxTarget:
    """Build a custom agent target that runs ``run-agent``.

    Returns:
        The target.
    """
    return BlackboxTarget(
        kind=BLACKBOX_TARGET_AGENT, harness=BLACKBOX_HARNESS_CUSTOM, model="m", run_command="run-agent"
    )


def _agent_scorer(runtime: FakeSandboxRuntime, scorer: Any = None) -> SandboxAgentScorer:
    """Wrap ``scorer`` in a sandbox agent scorer over ``runtime`` with the default no-retry policy.

    Args:
        runtime: The fake sandbox runtime.
        scorer: The user's scorer; a constant one when unset.

    Returns:
        The agent scorer.
    """
    scorer = scorer or (lambda candidate, case: (0.5, {"feedback": "ok"}))
    return SandboxAgentScorer(scorer, runtime=runtime, target=_agent_target(), gateway=_GATEWAY, job_id="job-1")


def _pairs(count: int) -> list[tuple[str, dict[str, int]]]:
    """Build one GEPA batch of ``count`` pairs on the same version.

    Args:
        count: Number of cases.

    Returns:
        The ``(candidate, case)`` pairs.
    """
    return [("hello world", {"i": index}) for index in range(count)]


def _run_batch(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
    server: EvalServer,
    pairs: list[tuple[Any, Any]],
    **ctx_overrides: Any,
) -> tuple[list[tuple[float, dict[str, Any]]], Any]:
    """Run the GEPA engine with a fake ``optimize_anything`` that scores one batch.

    Args:
        monkeypatch: Pytest monkeypatch fixture.
        tmp_path: Run directory.
        server: The budgeted scorer.
        pairs: The batch to send through ``batch_evaluator``.
        **ctx_overrides: Extra ``EngineContext`` fields, such as ``concurrency``.

    Returns:
        The batch results and the engine's result.
    """
    captured: dict[str, Any] = {}

    def fake_optimize_anything(**kwargs: Any) -> None:
        """Score one batch the way GEPA would, then report no result.

        Args:
            **kwargs: The engine's ``optimize_anything`` arguments.
        """
        token = _CALLER_TAG.set("caller")
        try:
            captured["results"] = kwargs["batch_evaluator"](pairs)
        finally:
            _CALLER_TAG.reset(token)

    monkeypatch.setattr(gepa_mod, "optimize_anything", fake_optimize_anything)
    task = Task(seed_candidate="hello world", objective="more vowels", cases=[case for _, case in pairs])
    result = GepaEngine().run(task, server, make_ctx(str(tmp_path), **ctx_overrides))
    return captured["results"], result


class _InFlightScorer:
    """Scorer that records how many calls overlap and holds each call until ``limit`` calls overlap.

    Args:
        limit: The overlap each call waits for before it returns.
        wait_seconds: The longest time one call waits for that overlap.
    """

    def __init__(self, limit: int, wait_seconds: float = 2.0) -> None:
        """Create the scorer."""
        self.limit = limit
        self.wait_seconds = wait_seconds
        self.in_flight = 0
        self.max_in_flight = 0
        self.calls = 0
        self.threads: set[int] = set()
        self._condition = threading.Condition()

    def __call__(self, candidate: Any, case: Any = None) -> tuple[float, dict[str, Any]]:
        """Count this call while it runs, and score the case by its index.

        Args:
            candidate: The version.
            case: The case.

        Returns:
            The case index as a score, and side information.
        """
        with self._condition:
            self.in_flight += 1
            self.calls += 1
            self.threads.add(threading.get_ident())
            self.max_in_flight = max(self.max_in_flight, self.in_flight)
            self._condition.notify_all()
            self._condition.wait_for(lambda: self.in_flight >= self.limit, timeout=self.wait_seconds)
            self.in_flight -= 1
        return float(case["i"]), {"feedback": f"case {case['i']}"}


def test_batch_calls_overlap_up_to_the_concurrency(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
    """A batch of agent cases runs ``ctx.concurrency`` calls at the same time.

    Args:
        monkeypatch: Pytest monkeypatch fixture.
        tmp_path: Run directory.
    """
    scorer = _InFlightScorer(limit=3)
    server = EvalServer(scorer, max_evals=16)

    _run_batch(monkeypatch, tmp_path, server, _pairs(3), concurrency=3)

    assert scorer.max_in_flight == 3
    assert scorer.calls == 3
    assert server.used == 3


def test_a_batch_larger_than_the_limit_never_goes_over_it(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
    """Six cases at concurrency two overlap two at a time and never three.

    Args:
        monkeypatch: Pytest monkeypatch fixture.
        tmp_path: Run directory.
    """
    scorer = _InFlightScorer(limit=3, wait_seconds=0.05)
    server = EvalServer(scorer, max_evals=16)

    results, _ = _run_batch(monkeypatch, tmp_path, server, _pairs(6), concurrency=2)

    assert scorer.max_in_flight == 2
    assert scorer.calls == 6
    assert [score for score, _ in results] == [0.0, 1.0, 2.0, 3.0, 4.0, 5.0]


def test_concurrency_one_keeps_the_serial_loop(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
    """The default concurrency scores every case on the caller thread, one at a time.

    Args:
        monkeypatch: Pytest monkeypatch fixture.
        tmp_path: Run directory.
    """
    scorer = _InFlightScorer(limit=2, wait_seconds=0.01)
    server = EvalServer(scorer, max_evals=16)

    _run_batch(monkeypatch, tmp_path, server, _pairs(4))

    assert scorer.max_in_flight == 1
    assert scorer.threads == {threading.get_ident()}


def test_parallel_results_keep_the_pair_order(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
    """Calls that finish out of order still return their results in pair order, as the serial loop does.

    Args:
        monkeypatch: Pytest monkeypatch fixture.
        tmp_path: Run directory.
    """
    finished: list[int] = []
    lock = threading.Lock()

    def slow_first(candidate: Any, case: Any = None) -> tuple[float, dict[str, Any]]:
        """Make earlier cases finish later, then score the case by its index.

        Args:
            candidate: The version.
            case: The case.

        Returns:
            The case index as a score, and side information.
        """
        time.sleep(0.02 * (4 - case["i"]))
        with lock:
            finished.append(case["i"])
        return float(case["i"]), {"feedback": f"case {case['i']}"}

    serial, _ = _run_batch(monkeypatch, tmp_path / "serial", EvalServer(slow_first, max_evals=16), _pairs(4))
    finished.clear()
    parallel, _ = _run_batch(
        monkeypatch, tmp_path / "parallel", EvalServer(slow_first, max_evals=16), _pairs(4), concurrency=4
    )

    assert finished != sorted(finished)
    assert parallel == serial
    assert [score for score, _ in parallel] == [0.0, 1.0, 2.0, 3.0]


def test_parallel_gepa_run_matches_the_serial_run(tmp_path: Path) -> None:
    """A real GEPA run picks the same version, score and tree at concurrency one and three.

    Args:
        tmp_path: Run directory.
    """
    cases = [{"i": 0}, {"i": 1}, {"i": 2}]
    results = []
    for concurrency in (1, 3):
        server = EvalServer(vowel_scorer, max_evals=16)
        task = Task(seed_candidate="hello world", objective="more vowels", cases=cases)
        ctx = make_ctx(str(tmp_path / str(concurrency)), concurrency=concurrency)
        results.append((GepaEngine().run(task, server, ctx), server.used))

    (serial, serial_used), (parallel, parallel_used) = results
    assert parallel.best_candidate == serial.best_candidate
    assert parallel.best_score == serial.best_score
    assert parallel.metadata["candidate_tree"] == serial.metadata["candidate_tree"]
    assert parallel_used == serial_used


def test_worker_threads_see_the_callers_context(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
    """A context value the caller sets reaches every parallel call.

    Args:
        monkeypatch: Pytest monkeypatch fixture.
        tmp_path: Run directory.
    """
    seen: list[str | None] = []

    def tagged(candidate: Any, case: Any = None) -> tuple[float, dict[str, Any]]:
        """Record the caller's context value.

        Args:
            candidate: The version.
            case: The case.

        Returns:
            A constant score and side information.
        """
        seen.append(_CALLER_TAG.get())
        return 0.5, {"feedback": "ok"}

    _run_batch(monkeypatch, tmp_path, EvalServer(tagged, max_evals=16), _pairs(4), concurrency=2)

    assert seen == ["caller"] * 4


def test_each_parallel_call_has_its_own_attempt_key(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
    """Every agent case opens one box under its own ``agent-run:<uuid>:attempt-1`` reservation key.

    Args:
        monkeypatch: Pytest monkeypatch fixture.
        tmp_path: Run directory.
    """
    runtime = FakeSandboxRuntime()
    server = EvalServer(_agent_scorer(runtime), max_evals=16)

    results, _ = _run_batch(monkeypatch, tmp_path, server, _pairs(4), concurrency=2)

    keys = [spec.operation_key for spec in runtime.specs]
    assert len(keys) == 4
    assert len(set(keys)) == 4
    assert all(_ATTEMPT_ONE_KEY.match(key) for key in keys)
    assert all(session.closed for session in runtime.sessions)
    assert [score for score, _ in results] == [0.5] * 4


def test_a_failed_parallel_call_is_not_retried(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
    """A box that dies is a metric error after one attempt, and its box is closed.

    Args:
        monkeypatch: Pytest monkeypatch fixture.
        tmp_path: Run directory.
    """

    def dying(command: str) -> CommandResult:
        """Kill the agent command; other commands succeed.

        Args:
            command: The shell command line.

        Returns:
            A clean exit for every command but the agent.

        Raises:
            SandboxInfraError: For the agent command.
        """
        if command == "run-agent":
            raise SandboxInfraError("host recycled")
        return CommandResult(exit_code=0)

    runtime = FakeSandboxRuntime(session_factory=lambda: FakeSandboxSession(script=dying))
    server = EvalServer(_agent_scorer(runtime), max_evals=16)

    results, _ = _run_batch(monkeypatch, tmp_path, server, _pairs(3), concurrency=3)

    assert len(runtime.sessions) == 3
    assert all(_ATTEMPT_ONE_KEY.match(spec.operation_key) for spec in runtime.specs)
    assert all(session.closed for session in runtime.sessions)
    assert all(info.get("status") == "metric_error" for _, info in results)


def test_a_budget_stop_starts_no_queued_call_and_leaks_no_box(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
    """A budget stop in one call ends the batch: queued cases never open a box, open boxes close.

    Args:
        monkeypatch: Pytest monkeypatch fixture.
        tmp_path: Run directory.
    """
    stop = threading.Event()

    def stopping(candidate: Any, case: Any = None) -> tuple[float, dict[str, Any]]:
        """Ask for a stop once the first case is scored.

        Args:
            candidate: The version.
            case: The agent's record.

        Returns:
            A constant score and side information.
        """
        stop.set()
        return 0.5, {"feedback": "ok"}

    def check_budget() -> None:
        """Raise a budget stop once a case asked for one.

        Raises:
            BudgetReached: After the first scored case.
        """
        if stop.is_set():
            raise BudgetReached

    runtime = FakeSandboxRuntime()
    server = EvalServer(_agent_scorer(runtime, stopping), max_evals=16)

    with pytest.raises(BudgetReached):
        _run_batch(monkeypatch, tmp_path, server, _pairs(6), concurrency=2, check_budget=check_budget)

    assert 1 <= len(runtime.sessions) <= 2
    assert all(session.closed for session in runtime.sessions)


def test_an_exhausted_server_ends_the_parallel_batch(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
    """When the scorer-run budget runs out mid-batch, the run returns the seed and no box leaks.

    Args:
        monkeypatch: Pytest monkeypatch fixture.
        tmp_path: Run directory.
    """
    runtime = FakeSandboxRuntime()
    server = EvalServer(_agent_scorer(runtime), max_evals=2)
    captured: dict[str, Any] = {}

    def fake_optimize_anything(**kwargs: Any) -> None:
        """Send a batch larger than the scorer-run budget.

        Args:
            **kwargs: The engine's ``optimize_anything`` arguments.
        """
        captured["results"] = kwargs["batch_evaluator"](_pairs(6))

    monkeypatch.setattr(gepa_mod, "optimize_anything", fake_optimize_anything)
    task = Task(seed_candidate="hello world", objective="more vowels", cases=[case for _, case in _pairs(6)])

    result = GepaEngine().run(task, server, make_ctx(str(tmp_path), concurrency=2))

    assert "results" not in captured
    assert result.best_candidate == "hello world"
    assert server.used == 2
    assert len(runtime.sessions) == 2
    assert all(session.closed for session in runtime.sessions)


_RUN_ID = "run-1"
_BOX_SPEC = SandboxSpec(lifetime_seconds=60, allowed_hosts=(), operation_key="op")
_CASES = [{"case": 0}, {"case": 1}]
_OPTIONS = NativeOptions(
    runtime="docker",
    model="m",
    gateway=_GATEWAY,
    max_token_cost=None,
    repo={"editable_paths": ["answer.txt"], "readonly_paths": [], "constrained": False},
)
_FILE_NOTE = re.compile(r"^answer\.txt:\n(.*)", re.DOTALL)


class _Stores:
    """The database, step log and tree store of one run."""

    def __init__(self, folder: Path) -> None:
        """Create the database with a funded wallet for ``alice`` and the running job.

        Args:
            folder: Where the database and the objects live.
        """
        folder.mkdir(parents=True, exist_ok=True)
        self.engine: Engine = create_engine(f"sqlite:///{folder / 'db.sqlite'}", connect_args={"timeout": 30})
        Base.metadata.create_all(self.engine)
        with Session(self.engine) as session:
            session.add(
                BillingCustomerModel(
                    username="alice", stripe_customer_id="local-alice", balance_cents=10_000, grant_remaining=0
                )
            )
            session.add(JobModel(optimization_id=_RUN_ID, username="alice", status="running", execution_generation=1))
            session.commit()
        self.step_log = StepLogStore(
            self.engine, LocalObjectStore(folder / "objects"), "worker-1", flush_window_seconds=0.02
        )
        self.trees = TreeStore(self.step_log)
        budgets = BudgetService(engine=self.engine)
        budget = budgets.create("alice", 1_000, idempotency_key="workspace-budget")
        budget = budgets.attach_to_job(budget.id, "alice", _RUN_ID, expected_revision=budget.revision)
        self.user = User("alice", engine=self.engine, budgets=budgets)
        WorkspaceStore(self.engine).set_budget(self.user.workspace().id, budget.id)
        self.budget_generation = budget.generation

    def tree(self, name: str, text: str) -> str:
        """Store one file as a tree.

        Args:
            name: The file name.
            text: The file text.

        Returns:
            The tree id.
        """
        blob = self.trees.put_blob(_RUN_ID, text.encode())
        return self.trees.put_tree(_RUN_ID, [TreeEntry(name, "file", blob)])

    def steps(self) -> list[tuple[int, str, int]]:
        """Return the root agent's committed steps.

        Returns:
            ``(seq, kind, execution_generation)`` for each step, in order.
        """
        with Session(self.engine) as session:
            rows = session.execute(
                select(AgentStepModel.seq, AgentStepModel.kind, AgentStepModel.execution_generation)
                .where(AgentStepModel.run_id == _RUN_ID, AgentStepModel.agent_id == ROOT_AGENT_ID)
                .order_by(AgentStepModel.seq)
            )
            return [(row.seq, row.kind, row.execution_generation) for row in rows]

    def new_generation(self) -> None:
        """Move the job to generation 2, as the orphan sweep does after a worker dies."""
        with Session(self.engine) as session:
            session.execute(update(JobModel).where(JobModel.optimization_id == _RUN_ID).values(execution_generation=2))
            session.commit()

    def close(self) -> None:
        """Close the step log and the database."""
        self.step_log.close()
        self.engine.dispose()


@pytest.fixture
def stores(tmp_path: Path) -> Iterator[_Stores]:
    """Yield the stores of one run.

    Args:
        tmp_path: Pytest's temporary folder.

    Yields:
        The stores.
    """
    created = _Stores(tmp_path / "stores")
    yield created
    created.close()


def _store(trees: TreeStore, files: dict[str, bytes]) -> str:
    """Store files as a tree straight in the tree store, as the caller of a loop stores a seed.

    ``put_files`` refuses a hidden path, so a seed that holds one is written here.

    Args:
        trees: The tree store.
        files: The file bytes by ``/``-separated path.

    Returns:
        The tree id.
    """
    root: dict[str, Any] = {}
    for path, data in files.items():
        *folders, name = path.split("/")
        level = root
        for folder in folders:
            level = level.setdefault(folder, {})
        level[name] = data

    def put(level: dict[str, Any]) -> str:
        """Store one folder and the folders below it.

        Args:
            level: The folder's entries by name: a nested folder, or the file bytes.

        Returns:
            The folder's tree id.
        """
        return trees.put_tree(
            _RUN_ID,
            [
                TreeEntry(name, "tree", put(value))
                if isinstance(value, dict)
                else TreeEntry(name, "file", trees.put_blob(_RUN_ID, value))
                for name, value in level.items()
            ],
        )

    return put(root)


class _WorkerDiedError(BaseException):
    """The worker dies in the middle of a model call: like a kill, no handler of ``Exception`` sees it."""


class _CountingSource(RuntimeBoxSource):
    """Open a local box for each acquire, and count the boxes that are open now and in total."""

    def __init__(self, counter: dict[str, int]) -> None:
        """Bind the source to a shared counter.

        Args:
            counter: Holds ``open`` and ``opened``.
        """
        super().__init__(LocalSubprocessRuntime(), _BOX_SPEC)
        self.counter = counter

    def acquire(self, agent_id: str, grants: Any) -> SandboxSession:
        """Open a box and count it.

        Args:
            agent_id: The agent that runs the step.
            grants: The step's grants.

        Returns:
            The box.
        """
        box = super().acquire(agent_id, grants)
        self.counter["open"] += 1
        self.counter["opened"] += 1
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
            self.counter["open"] -= 1


class _RepoModel:
    """A fake model: it runs one command, reads ``answer.txt``, then writes it back with one more ``x``."""

    def __init__(self, counter: dict[str, int], die_at: int | None = None) -> None:
        """Bind the model to the box counter.

        Args:
            counter: The box counter.
            die_at: The index of the call in which the worker dies; ``None`` never dies.
        """
        self.counter = counter
        self.die_at = die_at
        self.calls = 0
        self.open_boxes: list[int] = []

    def __call__(self, prompt: str | list[dict[str, Any]], tools: list[dict[str, Any]] | None = None) -> ModelReply:
        """Answer one turn of a proposal.

        Args:
            prompt: The proposal's messages.
            tools: Not used.

        Returns:
            The reply and its cost in cents.

        Raises:
            _WorkerDiedError: In the call at ``die_at``.
        """
        self.open_boxes.append(self.counter["open"])
        if self.calls == self.die_at:
            raise _WorkerDiedError
        self.calls += 1
        assert isinstance(prompt, list)
        last = str(prompt[-1]["content"])
        if last.startswith("Exit code"):
            return ModelReply("Let me read it.\n```read\nanswer.txt\n```", Decimal(1))
        if seen := _FILE_NOTE.match(last):
            return ModelReply(f"Here it is.\n```file answer.txt\n{seen[1].strip()}x\n```", Decimal(1))
        return ModelReply("First the size.\n```sh\nwc -c < answer.txt\n```", Decimal(1))


def _repo_run(
    stores: _Stores, run_dir: Path, *, generation: int, die_at: int | None = None
) -> tuple[Result, _RepoModel, dict[str, int], EngineLoop]:
    """Run repository GEPA on an engine loop over local boxes and the fake model.

    Args:
        stores: The run's stores.
        run_dir: The loop's local folder.
        generation: The job's execution generation.
        die_at: The model call in which the worker dies.

    Returns:
        The result, the model, the box counter and the loop.
    """
    run = stores.user.workspace().start_run(
        _RUN_ID,
        step_log=stores.step_log,
        execution_generation=generation,
        budget_generation=stores.budget_generation,
        grant_list_id="grants-1",
        chunk_cents=10,
        lease_slices=4,
    )
    counter = {"open": 0, "opened": 0}
    model = _RepoModel(counter, die_at=die_at)
    loop = EngineLoop(
        run,
        engine=stores.engine,
        trees=stores.trees,
        candidates=_CountingSource(counter),
        metrics=_CountingSource(counter),
        model=model,
        model_max_cents=2,
        run_dir=run_dir,
    )
    metric_tree = stores.tree("metric.txt", "len")

    def score(loop: EngineLoop, candidate: Any, case: Any) -> tuple[float | None, dict[str, Any]]:
        """Measure ``answer.txt`` of one version in a metric box.

        Args:
            loop: The engine loop.
            candidate: The version's tree id.
            case: Not used.

        Returns:
            The length of ``answer.txt`` and some feedback.
        """
        return loop.score(str(candidate), metric_tree, "wc -c < outputs/answer.txt"), {"feedback": "make it longer"}

    seed = RepoSeed(stores.tree("answer.txt", "a\n"), editable_paths=("answer.txt",))
    ctx = loop.context(native_options=_OPTIONS, repo_seed=seed)
    result = GepaEngine().run(Task("", cases=list(_CASES)), loop.server(score, max_evals=24), ctx)
    return result, model, counter, loop


def _no_native(*args: Any, **kwargs: Any) -> Result:
    """Fail when a repository run on the loop goes to the native runtime.

    Args:
        *args: Not used.
        **kwargs: Not used.

    Raises:
        AssertionError: Always.
    """
    raise AssertionError("a repository GEPA run on the loop called run_native_engine")


def test_a_repository_run_opens_boxes_only_for_commands_and_scoring(
    monkeypatch: pytest.MonkeyPatch, stores: _Stores, tmp_path: Path
) -> None:
    """Run repository GEPA on the loop: no box for a model call or a file, and one step for each reply and command.

    Args:
        monkeypatch: Pytest monkeypatch fixture.
        stores: The stores of the run.
        tmp_path: Pytest's temporary folder.
    """
    monkeypatch.setattr(gepa_mod, "run_native_engine", _no_native)

    result, model, counter, loop = _repo_run(stores, tmp_path / "loop", generation=1)

    answer = loop.read_file(str(result.best_candidate), "answer.txt")
    assert answer is not None
    assert answer.startswith(b"axx")
    assert result.best_score == len(answer)
    assert model.calls > 0
    assert model.open_boxes == [0] * model.calls
    assert counter["open"] == 0
    kinds = [kind for _, kind, _ in stores.steps()]
    assert kinds.count("model") == model.calls
    # Each proposal runs one command; the read and the write open no box and commit no step.
    assert kinds.count("command") == counter["opened"]
    assert [seq for seq, _, _ in stores.steps()] == list(range(len(kinds)))


def test_a_killed_repository_run_resumes_to_the_population_of_a_clean_run(
    monkeypatch: pytest.MonkeyPatch, stores: _Stores, tmp_path: Path
) -> None:
    """Kill the worker in the middle of the second proposal, and resume it in the same folder.

    Args:
        monkeypatch: Pytest monkeypatch fixture.
        stores: The stores of the run.
        tmp_path: Pytest's temporary folder.
    """
    monkeypatch.setattr(gepa_mod, "run_native_engine", _no_native)
    clean_stores = _Stores(tmp_path / "clean")
    try:
        clean, clean_model, _, _ = _repo_run(clean_stores, tmp_path / "clean-loop", generation=1)
        clean_steps = [kind for _, kind, _ in clean_stores.steps()]
    finally:
        clean_stores.close()

    # Call 4 is the second proposal's read reply: its command is already committed.
    with pytest.raises(_WorkerDiedError):
        _repo_run(stores, tmp_path / "loop", generation=1, die_at=4)
    committed = len(stores.steps())
    stores.new_generation()
    resumed, model, _, _ = _repo_run(stores, tmp_path / "loop", generation=2)

    # A tree id is the hash of its content, so the two databases give the same ids.
    assert resumed.best_candidate == clean.best_candidate
    assert resumed.best_score == clean.best_score
    assert resumed.metadata["candidate_tree"] == clean.metadata["candidate_tree"]
    assert len(resumed.metadata["candidate_tree"]) > 2
    steps = stores.steps()
    assert [kind for _, kind, _ in steps] == clean_steps
    assert [generation for _, _, generation in steps] == [1] * committed + [2] * (len(steps) - committed)
    # The committed replies come back from the log; only the calls past it reach the model.
    assert model.calls == clean_model.calls - 4


class _FakeCalls:
    """The loop's calls for the proposer: scripted model replies, and files and commands that run nowhere."""

    def __init__(self, replies: list[str], cents: int = 1) -> None:
        """Set the model's replies in order, and the cost of each one.

        Args:
            replies: The model's replies.
            cents: The cost of each reply in cents.
        """
        self.replies = replies
        self.cents = cents
        self.prompts: list[str] = []
        self.commands: list[str] = []
        self.reads: list[str] = []
        self.listings: list[str] = []
        self.writes: list[tuple[dict[str, bytes], str | None]] = []

    def model(self, prompt: list[dict[str, Any]], tools: list[dict[str, Any]] | None) -> ModelReply:
        """Return the next reply.

        Args:
            prompt: The messages.
            tools: Not used.

        Returns:
            The reply.
        """
        self.prompts.append(str(prompt[-1]["content"]))
        return ModelReply(self.replies.pop(0), Decimal(self.cents))

    def run(self, tree_id: str, command: str, grants: tuple[Any, ...]) -> tuple[CommandResult, str]:
        """Record a command.

        Args:
            tree_id: Not used.
            command: The command.
            grants: Not used.

        Returns:
            A passing result and a new tree id.
        """
        self.commands.append(command)
        return CommandResult(0, "listed", ""), "tree-command"

    def put(self, files: dict[str, bytes], base_tree_id: str | None) -> str:
        """Record a tree write.

        Args:
            files: The files.
            base_tree_id: The base tree.

        Returns:
            A new tree id.
        """
        self.writes.append((dict(files), base_tree_id))
        return f"tree-{len(self.writes)}"

    def read(self, tree_id: str, path: str) -> bytes | None:
        """Record a file read.

        Args:
            tree_id: Not used.
            path: The path.

        Returns:
            The file, or ``None`` for a path other than ``answer.txt``, as the loop gives for a hidden path.
        """
        self.reads.append(path)
        return b"a\n" if path == "answer.txt" else None

    def list_files(self, tree_id: str, path: str = "") -> list[str] | None:
        """Record a folder listing.

        Args:
            tree_id: Not used.
            path: The folder.

        Returns:
            The files under the root or ``src``, or ``None`` for another folder.

        Raises:
            LookupError: For an id that does not start with ``tree-``, as the loop does for a tree it cannot see.
        """
        if not tree_id.startswith("tree-"):
            raise LookupError(f"run cannot see tree {tree_id}")
        self.listings.append(path)
        return {"": ["answer.txt", "src/a.py"], "src": ["src/a.py"]}.get(path)

    def ctx(self, tmp_path: Path, **overrides: Any) -> Any:
        """Build an engine context over these calls and a seed with each kind of path rule.

        Args:
            tmp_path: The run folder.
            **overrides: Other context fields.

        Returns:
            The context.
        """
        seed = RepoSeed(
            "tree-seed",
            editable_paths=("answer.txt", "src"),
            readonly_paths=("src/vendor",),
            constrained_paths=("src/rules.toml",),
            hidden_hashes=hidden_hashes(["eval/metric.py", "secret"]),
        )
        fields = {
            "call_model": self.model,
            "run_command": self.run,
            "put_files": self.put,
            "read_file": self.read,
            "list_files": self.list_files,
        }
        return make_ctx(str(tmp_path), **{**fields, "repo_seed": seed, **overrides})


def _file(path: str, text: str = "new\n") -> str:
    """Return a reply that writes one file.

    Args:
        path: The file path.
        text: The file text.

    Returns:
        The reply.
    """
    return f"```file {path}\n{text}```"


def test_the_proposer_refuses_each_path_rule_and_writes_a_valid_version_with_no_box(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    """Refuse hidden, protected, read-only, outside and invalid paths; then write the valid files over the parent.

    Args:
        monkeypatch: Pytest monkeypatch fixture.
        tmp_path: Pytest's temporary folder.
    """
    calls = _FakeCalls(
        [
            _file("eval/metric.py"),
            _file("secret/key.txt"),
            _file(".optimize/exploits/a.py"),
            _file("src/rules.toml"),
            _file("src/vendor/lib.py"),
            _file("../escape.txt") + "\n" + _file("README.md"),
            "````file answer.txt\n```sh\nls\n```\n````\n" + _file("src/new.py"),
        ]
    )
    proposer = gepa_mod._LoopRepoProposer.bind(calls.ctx(tmp_path), Task(""))
    assert proposer is not None
    monkeypatch.setattr(gepa_mod, "_MAX_TURNS", 7)

    assert proposer({"c": "tree-parent"}, {"c": []}, ["c"]) == {"c": "tree-1"}

    notes = calls.prompts[1:]
    # The hidden paths and the exploits folder are outside the editable paths, so they get that answer.
    assert notes[0] == "The files break the editing rules: 'eval/metric.py' is outside the editable paths."
    assert notes[1] == "The files break the editing rules: 'secret/key.txt' is outside the editable paths."
    assert notes[2] == "The files break the editing rules: '.optimize/exploits/a.py' is outside the editable paths."
    assert "protected by the repository rules" in notes[3]
    assert "read-only" in notes[4]
    assert "'../escape.txt' is not a path inside the repository." in notes[5]
    assert "'README.md' is outside the editable paths." in notes[5]
    assert calls.writes == [({"answer.txt": b"```sh\nls\n```\n", "src/new.py": b"new\n"}, "tree-parent")]
    assert calls.commands == []
    assert proposer.total_cost == pytest.approx(0.07)


def test_the_proposer_lists_and_reads_with_no_box_and_keeps_the_parent_when_turns_run_out(
    tmp_path: Path,
) -> None:
    """List folders and read a file with no box, run one command, and keep the parent with no version.

    Args:
        tmp_path: Pytest's temporary folder.
    """
    replies = ["```ls\n```", "```ls\nsrc\n```", "```ls\nnone\n```", "```read\nanswer.txt\n```", "```sh\nls\n```"]
    calls = _FakeCalls([*replies, "no block"])
    proposer = gepa_mod._LoopRepoProposer.bind(calls.ctx(tmp_path), Task(""))
    assert proposer is not None

    assert proposer({"c": "tree-parent"}, {"c": []}, ["c"]) == {"c": "tree-parent"}
    assert calls.prompts[1] == "Files (2):\nanswer.txt\nsrc/a.py"
    assert calls.prompts[2] == "Files (1):\nsrc/a.py"
    assert calls.prompts[3] == "'none' is not a folder of the current version."
    assert calls.prompts[4] == "answer.txt:\na\n"
    assert calls.prompts[5].startswith("Exit code 0.\nstdout:\nlisted")
    assert calls.listings == ["", "src", "none"]
    assert calls.reads == ["answer.txt"]
    assert calls.commands == ["ls\n"]
    assert calls.writes == []


def test_the_reflection_cost_cap_stops_the_repository_run(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
    """GEPA's cost stopper reads the committed cost of the proposer's replies.

    Args:
        monkeypatch: Pytest monkeypatch fixture.
        tmp_path: Pytest's temporary folder.
    """
    monkeypatch.setattr(gepa_mod, "run_native_engine", _no_native)
    calls = _FakeCalls([_file("answer.txt", f"{'x' * n}\n") for n in range(1, 40)], cents=5)
    server = EvalServer(lambda candidate, case: (float(len(str(candidate))), {}), max_evals=200)

    GepaEngine().run(Task("", cases=list(_CASES)), server, calls.ctx(tmp_path, proposer_token_budget_usd=0.12))

    # Each proposal costs 5 cents, so the 12-cent cap stops GEPA after its third proposal.
    assert len(calls.writes) == 3


def test_a_loop_stop_in_the_proposer_stops_the_repository_run(tmp_path: Path) -> None:
    """GEPA logs a proposer error and goes on, so the engine raises the loop's stop after GEPA returns.

    Args:
        tmp_path: Pytest's temporary folder.
    """

    def stopped(prompt: Any, tools: Any) -> ModelReply:
        """Stop, as a fenced or closed loop does.

        Args:
            prompt: Not used.
            tools: Not used.

        Raises:
            LoopStoppedError: Always.
        """
        raise LoopStoppedError("Run run-1 is closed.")

    server = EvalServer(lambda candidate, case: (float(len(str(candidate))), {}), max_evals=40)
    ctx = _FakeCalls([]).ctx(tmp_path, call_model=None)
    ctx.call_model = stopped

    with pytest.raises(LoopStoppedError):
        GepaEngine().run(Task("", cases=list(_CASES)), server, ctx)
    assert server.used < 40


def test_a_repository_run_with_no_loop_keeps_the_native_runtime(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    """Until the cutover (SKY-728), a repository run with no loop calls or no seed runs in the native runtime.

    Args:
        monkeypatch: Pytest monkeypatch fixture.
        tmp_path: Pytest's temporary folder.
    """
    native = Result(best_candidate="patch", best_score=1.0, total_evals=0)
    monkeypatch.setattr(gepa_mod, "run_native_engine", lambda *args: native)
    server = EvalServer(lambda candidate, case: (1.0, {}), max_evals=4)
    no_calls = _FakeCalls([]).ctx(tmp_path, native_options=_OPTIONS, put_files=None)
    no_seed = make_ctx(str(tmp_path), native_options=_OPTIONS)

    assert GepaEngine().run(Task(""), server, no_calls) is native
    assert GepaEngine().run(Task(""), server, no_seed) is native


@pytest.mark.parametrize(
    "path", [".optimize", ".OPTIMIZE", ".optimize/exploits", ".Optimize/Exploits", ".optimize/EXPLOITS/a.py"]
)
def test_the_proposer_refuses_the_exploits_folder_its_parents_and_their_case_variants(
    path: str, tmp_path: Path
) -> None:
    """A file at a parent of the exploits folder would block the hidden suite, so each case variant is refused.

    Args:
        path: The path the model writes.
        tmp_path: Pytest's temporary folder.
    """
    calls = _FakeCalls([_file(path)] + ["no block"] * (gepa_mod._MAX_TURNS - 1))
    ctx = calls.ctx(tmp_path, repo_seed=RepoSeed("tree-seed", editable_paths=(".",)))
    proposer = gepa_mod._LoopRepoProposer.bind(ctx, Task(""))
    assert proposer is not None

    assert proposer({"c": "tree-parent"}, {"c": []}, ["c"]) == {"c": "tree-parent"}
    assert calls.prompts[1] == f"The files break the editing rules: '{path}' may not be changed."
    assert calls.writes == []


def test_the_proposer_writes_a_path_that_only_starts_like_the_exploits_folder(tmp_path: Path) -> None:
    """``.optimizer`` and ``.optimize/exploits2`` are neither the exploits folder nor one of its parents.

    Args:
        tmp_path: Pytest's temporary folder.
    """
    calls = _FakeCalls([_file(".optimizer") + "\n" + _file(".optimize/exploits2/a.py")])
    ctx = calls.ctx(tmp_path, repo_seed=RepoSeed("tree-seed", editable_paths=(".",)))
    proposer = gepa_mod._LoopRepoProposer.bind(ctx, Task(""))
    assert proposer is not None

    assert proposer({"c": "tree-parent"}, {"c": []}, ["c"]) == {"c": "tree-1"}
    assert calls.writes == [({".optimizer": b"new\n", ".optimize/exploits2/a.py": b"new\n"}, "tree-parent")]


def test_a_hidden_file_reaches_the_proposer_through_no_tool_on_a_real_loop(
    monkeypatch: pytest.MonkeyPatch, stores: _Stores, tmp_path: Path
) -> None:
    """On the loop, no ``ls``, ``read``, shell ``cat`` or write shows ``eval/`` or ``optimize.toml`` to the engine.

    Args:
        monkeypatch: Pytest's monkeypatch fixture.
        stores: The stores of the run.
        tmp_path: Pytest's temporary folder.
    """
    metric, config = "the-hidden-metric-42", "the-hidden-config-43"
    run = stores.user.workspace().start_run(
        _RUN_ID,
        step_log=stores.step_log,
        execution_generation=1,
        budget_generation=stores.budget_generation,
        grant_list_id="grants-1",
        chunk_cents=10,
        lease_slices=4,
    )
    counter = {"open": 0, "opened": 0}
    replies = [
        "```ls\n```",
        "```ls\neval\n```",
        "```read\neval/metric.py\n```",
        "```read\noptimize.toml\n```",
        "```sh\ncat eval/metric.py optimize.toml\n```",
        _file("optimize.toml"),
        "no block",
    ]
    calls = _FakeCalls(replies)
    hidden = hidden_hashes(["eval", "optimize.toml"])
    loop = EngineLoop(
        run,
        engine=stores.engine,
        trees=stores.trees,
        candidates=_CountingSource(counter),
        metrics=_CountingSource(counter),
        model=calls.model,
        model_max_cents=2,
        run_dir=tmp_path / "loop",
        hidden_hashes=hidden,
    )
    raw_tree = _store(
        stores.trees, {"answer.txt": b"a\n", "eval/metric.py": metric.encode(), "optimize.toml": config.encode()}
    )
    ctx = loop.context(repo_seed=RepoSeed(raw_tree, editable_paths=("answer.txt",), hidden_hashes=hidden))
    assert ctx.repo_seed is not None
    seed_tree = ctx.repo_seed.tree_id
    proposer = gepa_mod._LoopRepoProposer.bind(ctx, Task(""))
    assert proposer is not None

    monkeypatch.setattr(gepa_mod, "_MAX_TURNS", 7)

    assert proposer({"c": seed_tree}, {"c": []}, ["c"]) == {"c": seed_tree}
    notes = calls.prompts[1:]
    assert notes[0] == "Files (1):\nanswer.txt"
    assert notes[1] == "'eval' is not a folder of the current version."
    assert notes[2] == "'eval/metric.py' is not a file of the current version."
    assert notes[3] == "'optimize.toml' is not a file of the current version."
    assert notes[4].startswith("Exit code ")
    assert not notes[4].startswith("Exit code 0.")
    assert notes[5] == "The files break the editing rules: 'optimize.toml' is outside the editable paths."
    assert all(metric not in note and config not in note for note in calls.prompts)
    assert counter["opened"] == 1


def test_a_file_at_a_folder_that_holds_only_hidden_files_writes_as_at_a_missing_path_on_a_real_loop(
    stores: _Stores, tmp_path: Path
) -> None:
    """The engine's seed tree has no ``eval/``, so a file at ``eval`` writes as at any missing path.

    The visible tree never holds ``eval/metric.py``, so the write drops no hidden file and the version holds none.

    Args:
        stores: The stores of the run.
        tmp_path: Pytest's temporary folder.
    """
    run = stores.user.workspace().start_run(
        _RUN_ID,
        step_log=stores.step_log,
        execution_generation=1,
        budget_generation=stores.budget_generation,
        grant_list_id="grants-1",
        chunk_cents=10,
        lease_slices=4,
    )
    counter = {"open": 0, "opened": 0}
    calls = _FakeCalls([_file("eval")] + ["no block"] * (gepa_mod._MAX_TURNS - 1))
    hidden = hidden_hashes(["eval/metric.py"])
    loop = EngineLoop(
        run,
        engine=stores.engine,
        trees=stores.trees,
        candidates=_CountingSource(counter),
        metrics=_CountingSource(counter),
        model=calls.model,
        model_max_cents=2,
        run_dir=tmp_path / "loop",
        hidden_hashes=hidden,
    )
    raw_tree = _store(stores.trees, {"answer.txt": b"a\n", "eval/metric.py": b"metric"})
    ctx = loop.context(repo_seed=RepoSeed(raw_tree, editable_paths=(".",), hidden_hashes=hidden))
    assert ctx.repo_seed is not None
    seed_tree = ctx.repo_seed.tree_id
    proposer = gepa_mod._LoopRepoProposer.bind(ctx, Task(""))
    assert proposer is not None

    written = proposer({"c": seed_tree}, {"c": []}, ["c"])["c"]
    assert written != seed_tree
    assert loop.list_files(written) == ["answer.txt", "eval"]
    assert loop.read_file(written, "eval") == b"new\n"
    assert len(calls.prompts) == 1
    assert counter["opened"] == 0


def test_a_file_at_a_parent_of_a_protected_or_read_only_path_is_refused(tmp_path: Path) -> None:
    """A file at ``cfg`` or ``lib`` would replace the protected or read-only path under it.

    Args:
        tmp_path: Pytest's temporary folder.
    """
    calls = _FakeCalls([_file("cfg") + "\n" + _file("lib")] + ["no block"] * (gepa_mod._MAX_TURNS - 1))
    seed = RepoSeed(
        "tree-seed", editable_paths=(".",), constrained_paths=("cfg/rules.toml",), readonly_paths=("lib/sub",)
    )
    proposer = gepa_mod._LoopRepoProposer.bind(calls.ctx(tmp_path, repo_seed=seed), Task(""))
    assert proposer is not None

    assert proposer({"c": "tree-parent"}, {"c": []}, ["c"]) == {"c": "tree-parent"}
    assert calls.prompts[1] == "The files break the editing rules: 'cfg' may not be changed. 'lib' may not be changed."
    assert calls.writes == []


def test_the_brief_names_no_hidden_protected_or_read_only_path(tmp_path: Path) -> None:
    """The brief lists the visible protected and read-only paths, and leaves out the hidden ones.

    Args:
        tmp_path: Pytest's temporary folder.
    """
    calls = _FakeCalls([_file("answer.txt")])
    seed = RepoSeed(
        "tree-seed",
        editable_paths=("answer.txt",),
        constrained_paths=("optimize.toml", "src/rules.toml"),
        readonly_paths=("eval/data", "vendor"),
        hidden_hashes=hidden_hashes(["optimize.toml", "eval"]),
    )
    proposer = gepa_mod._LoopRepoProposer.bind(calls.ctx(tmp_path, repo_seed=seed), Task(""))
    assert proposer is not None

    proposer({"c": "tree-parent"}, {"c": []}, ["c"])
    assert "The repository rules protect: src/rules.toml." in calls.prompts[0]
    assert "Submodules and Git LFS files stay as fetched: vendor." in calls.prompts[0]
    assert "optimize.toml" not in calls.prompts[0]
    assert "eval/data" not in calls.prompts[0]


def test_a_cut_read_or_listing_says_it_was_cut(tmp_path: Path) -> None:
    """A long file or listing ends with a notice, so the model does not write back a part as the whole file.

    Args:
        tmp_path: Pytest's temporary folder.
    """
    calls = _FakeCalls(["```read\nlong.txt\n```", "```ls\n```"] + ["no block"] * (gepa_mod._MAX_TURNS - 2))
    names = [f"f{index:04}" for index in range(1000)]
    ctx = calls.ctx(tmp_path, read_file=lambda tree_id, path: b"x" * 5000, list_files=lambda tree_id, path="": names)
    proposer = gepa_mod._LoopRepoProposer.bind(ctx, Task(""))
    assert proposer is not None

    proposer({"c": "tree-parent"}, {"c": []}, ["c"])
    assert calls.prompts[1] == "long.txt:\n" + "x" * 4000 + "\n[cut at 4000 of 5000 characters; read the rest with sh]"
    assert calls.prompts[2].endswith("\n[cut at 4000 of 5999 characters; read the rest with sh]")
    assert calls.prompts[2].startswith("Files (1000):\nf0000\n")


def test_a_repository_run_with_no_evals_left_returns_the_seed_tree(tmp_path: Path) -> None:
    """On the loop a version is a tree id, so a run with no task seed and no scorer calls left returns the seed tree.

    Args:
        tmp_path: Pytest's temporary folder.
    """
    ctx = _FakeCalls([]).ctx(tmp_path)
    server = EvalServer(lambda candidate, case: (1.0, {}), max_evals=0)

    result = GepaEngine().run(Task("", cases=list(_CASES)), server, ctx)

    assert result.best_candidate == "tree-seed"


def test_a_repository_run_that_gepa_ends_with_no_result_returns_the_seed_tree(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    """When GEPA stops with no saved state, a loop run with no task seed returns the run's seed tree.

    Args:
        monkeypatch: Pytest monkeypatch fixture.
        tmp_path: Pytest's temporary folder.
    """

    def exhausted(**kwargs: Any) -> None:
        """Stop as the scorer cap does before GEPA saves a state.

        Args:
            **kwargs: Not used.

        Raises:
            BudgetExhaustedError: Always.
        """
        raise BudgetExhaustedError

    monkeypatch.setattr(gepa_mod, "optimize_anything", exhausted)
    ctx = _FakeCalls([]).ctx(tmp_path)
    server = EvalServer(lambda candidate, case: (1.0, {}), max_evals=4)

    result = GepaEngine().run(Task("", cases=list(_CASES)), server, ctx)

    assert result.best_candidate == "tree-seed"


def test_a_repository_run_starts_from_the_task_seed_when_it_is_a_tree_of_the_run(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    """Auto's continuation gives the winner's tree as the task seed, so GEPA starts and falls back there.

    Args:
        monkeypatch: Pytest monkeypatch fixture.
        tmp_path: Pytest's temporary folder.
    """
    seeds: list[Any] = []

    def exhausted(**kwargs: Any) -> None:
        """Record GEPA's seed, then stop as the scorer cap does before GEPA saves a state.

        Args:
            **kwargs: GEPA's arguments.

        Raises:
            BudgetExhaustedError: Always.
        """
        seeds.append(kwargs["seed_candidate"])
        raise BudgetExhaustedError

    monkeypatch.setattr(gepa_mod, "optimize_anything", exhausted)
    task = Task("tree-winner", cases=list(_CASES))
    no_evals = EvalServer(lambda candidate, case: (1.0, {}), max_evals=0)
    server = EvalServer(lambda candidate, case: (1.0, {}), max_evals=4)

    assert GepaEngine().run(task, no_evals, _FakeCalls([]).ctx(tmp_path)).best_candidate == "tree-winner"
    assert GepaEngine().run(task, server, _FakeCalls([]).ctx(tmp_path)).best_candidate == "tree-winner"
    assert seeds == ["tree-winner"]


def test_the_start_tree_is_the_task_seed_only_when_the_real_loop_can_see_it(stores: _Stores, tmp_path: Path) -> None:
    """On a real loop, a tree id of the run is the start, empty and ``None`` give the seed tree, and text fails.

    Args:
        stores: The stores of the run.
        tmp_path: Pytest's temporary folder.
    """
    run = stores.user.workspace().start_run(
        _RUN_ID,
        step_log=stores.step_log,
        execution_generation=1,
        budget_generation=stores.budget_generation,
        grant_list_id="grants-1",
        chunk_cents=10,
        lease_slices=4,
    )
    counter = {"open": 0, "opened": 0}
    loop = EngineLoop(
        run,
        engine=stores.engine,
        trees=stores.trees,
        candidates=_CountingSource(counter),
        metrics=_CountingSource(counter),
        model=_FakeCalls([]).model,
        model_max_cents=2,
        run_dir=tmp_path / "loop",
    )
    seed_tree = loop.put_files({"answer.txt": b"a\n"})
    winner = loop.put_files({"answer.txt": b"axx\n"})
    ctx = loop.context(repo_seed=RepoSeed(seed_tree, editable_paths=("answer.txt",)))

    def start(seed: str | None) -> str:
        """Return the start tree for one task seed.

        Args:
            seed: The task's seed.

        Returns:
            The start tree.
        """
        proposer = gepa_mod._LoopRepoProposer.bind(ctx, Task(seed))
        assert proposer is not None
        return proposer.start_tree()

    assert start(winner) == winner
    # A silent fallback would optimize the run's seed in place of the version that the caller gave.
    with pytest.raises(ServiceError, match="not a version of this run"):
        start("text seed")
    with pytest.raises(ServiceError, match="not a version of this run"):
        start("0" * 64)
    assert start("") == seed_tree
    assert start(None) == seed_tree
    assert counter["opened"] == 0


def test_a_hidden_path_outside_the_editable_paths_gets_the_answer_of_any_other_path(tmp_path: Path) -> None:
    """A hidden name and a plain name outside the editable paths get the same answer, so it does not show which is hidden.

    Args:
        tmp_path: Pytest's temporary folder.
    """
    calls = _FakeCalls([])
    proposer = gepa_mod._LoopRepoProposer.bind(calls.ctx(tmp_path), Task(""))
    assert proposer is not None

    for hidden, plain in (("secret", "public"), ("secret/key.txt", "public/key.txt"), ("eval/metric.py", "docs/a.py")):
        assert proposer.hidden(hidden)
        assert not proposer.hidden(plain)
        hidden_note, plain_note = proposer.path_problem(hidden), proposer.path_problem(plain)
        assert hidden_note is not None
        assert plain_note is not None
        assert hidden_note.replace(hidden, "<path>") == plain_note.replace(plain, "<path>")


def test_a_command_whose_output_is_rejected_gets_a_fixed_note_and_the_proposal_goes_on(tmp_path: Path) -> None:
    """The rejection names the hidden path, so the model gets a fixed note; the next reply still runs.

    Args:
        tmp_path: Pytest's temporary folder.
    """
    rejected: list[str] = []

    def reject(tree_id: str, command: str, grants: tuple[Any, ...]) -> tuple[CommandResult, str]:
        """Reject the command's output tree, as the loop does for an output with a hidden path.

        Args:
            tree_id: Not used.
            command: The command.
            grants: Not used.

        Raises:
            TreeRejectedError: Always, with the hidden path in its text.
        """
        rejected.append(command)
        raise TreeRejectedError("run-1:root:3", "the output tree holds the hidden path eval/metric.py")

    def notes() -> list[str]:
        """Run one proposal whose first reply runs a command and whose second writes a file.

        Returns:
            The notes the model got.
        """
        calls = _FakeCalls(["```sh\necho x > eval/metric.py\n```", _file("answer.txt")])
        proposer = gepa_mod._LoopRepoProposer.bind(calls.ctx(tmp_path, run_command=reject), Task(""))
        assert proposer is not None
        assert proposer({"c": "tree-parent"}, {"c": []}, ["c"]) == {"c": "tree-1"}
        return calls.prompts[1:]

    first = notes()
    assert first == ["The command wrote a path that it may not write. Its output was discarded."]
    assert all("eval" not in note for note in first)
    # The note does not depend on the error's text, so a replay that raises the error again gives the same note.
    assert notes() == first
    assert len(rejected) == 2


def test_a_loop_stop_in_a_proposer_command_ends_the_proposal(tmp_path: Path) -> None:
    """A loop stop from a command is not a box error, so the proposer keeps it and raises it.

    Args:
        tmp_path: Pytest's temporary folder.
    """

    def stopped(tree_id: str, command: str, grants: tuple[Any, ...]) -> tuple[CommandResult, str]:
        """Stop, as a fenced or closed loop does.

        Args:
            tree_id: Not used.
            command: Not used.
            grants: Not used.

        Raises:
            LoopStoppedError: Always.
        """
        raise LoopStoppedError("Run run-1 is closed.")

    calls = _FakeCalls(["```sh\nls\n```", _file("answer.txt")])
    proposer = gepa_mod._LoopRepoProposer.bind(calls.ctx(tmp_path, run_command=stopped), Task(""))
    assert proposer is not None

    with pytest.raises(LoopStoppedError):
        proposer({"c": "tree-parent"}, {"c": []}, ["c"])
    assert isinstance(proposer.failure, LoopStoppedError)
    assert calls.writes == []


def test_a_folder_whose_files_are_all_hidden_looks_missing_on_a_real_loop(stores: _Stores, tmp_path: Path) -> None:
    """``ls eval`` answers as for a missing folder, as the engine's seed tree has no ``eval/``.

    Args:
        stores: The stores of the run.
        tmp_path: Pytest's temporary folder.
    """
    run = stores.user.workspace().start_run(
        _RUN_ID,
        step_log=stores.step_log,
        execution_generation=1,
        budget_generation=stores.budget_generation,
        grant_list_id="grants-1",
        chunk_cents=10,
        lease_slices=4,
    )
    counter = {"open": 0, "opened": 0}
    replies = ["```ls\neval\n```", "```ls\nnone\n```", "no block", "no block", "no block", "no block"]
    calls = _FakeCalls(replies)
    hidden = hidden_hashes(["eval/metric.py"])
    loop = EngineLoop(
        run,
        engine=stores.engine,
        trees=stores.trees,
        candidates=_CountingSource(counter),
        metrics=_CountingSource(counter),
        model=calls.model,
        model_max_cents=2,
        run_dir=tmp_path / "loop",
        hidden_hashes=hidden,
    )
    raw_tree = _store(stores.trees, {"answer.txt": b"a\n", "eval/metric.py": b"metric"})
    ctx = loop.context(repo_seed=RepoSeed(raw_tree, editable_paths=("answer.txt",), hidden_hashes=hidden))
    assert ctx.repo_seed is not None
    seed_tree = ctx.repo_seed.tree_id
    proposer = gepa_mod._LoopRepoProposer.bind(ctx, Task(""))
    assert proposer is not None

    assert proposer({"c": seed_tree}, {"c": []}, ["c"]) == {"c": seed_tree}
    notes = calls.prompts[1:]
    assert notes[0] == "'eval' is not a folder of the current version."
    assert notes[0].replace("eval", "<path>") == notes[1].replace("none", "<path>")
    assert counter["opened"] == 0


def test_a_failed_model_call_keeps_the_parent_and_gepa_does_not_pay_for_it_again(tmp_path: Path) -> None:
    """A failed reply ends only its proposal, so GEPA's batch retry does not call the model for it again.

    Args:
        tmp_path: Pytest's temporary folder.
    """
    calls = _FakeCalls([_file("answer.txt", f"{'x' * n}\n") for n in range(1, 40)])
    model_calls: list[tuple[int | None, int]] = []

    def model(prompt: list[dict[str, Any]], tools: list[dict[str, Any]] | None) -> ModelReply:
        """Fail the first reply as a loop model call does, then write a longer file each time.

        Args:
            prompt: The messages.
            tools: Not used.

        Returns:
            The reply.

        Raises:
            ModelCallFailedError: In the first call.
        """
        model_calls.append((gepa_mod.current_proposal_iteration(), len(prompt)))
        if len(model_calls) == 1:
            raise ModelCallFailedError("TimeoutError")
        return calls.model(prompt, tools)

    proposer = gepa_mod._LoopRepoProposer.bind(calls.ctx(tmp_path, call_model=model), Task(""))
    assert proposer is not None
    assert proposer({"c": "tree-parent"}, {"c": []}, ["c"]) == {"c": "tree-parent"}
    assert len(model_calls) == 1
    assert proposer.failure is None

    model_calls.clear()
    server = EvalServer(lambda candidate, case: (float(len(str(candidate))), {}), max_evals=12)
    result = GepaEngine().run(Task("", cases=list(_CASES)), server, calls.ctx(tmp_path, call_model=model))

    # Each proposal opens with one brief; a retry of the failed one would open a second in the same iteration.
    briefs = [iteration for iteration, length in model_calls if length == 1]
    assert len(briefs) == len(set(briefs)) > 1
    assert calls.writes
    assert result.total_evals == 12


def test_a_refused_grant_list_in_a_proposer_command_stops_the_proposal(tmp_path: Path) -> None:
    """A grant list with no network policy fails each later command too, so the proposer keeps the error and raises it.

    Args:
        tmp_path: Pytest's temporary folder.
    """

    def denied(tree_id: str, command: str, grants: tuple[Any, ...]) -> tuple[CommandResult, str]:
        """Refuse the grants, as the loop does for a grant list that does not resolve to a network policy.

        Args:
            tree_id: Not used.
            command: Not used.
            grants: Not used.

        Raises:
            GrantDeniedError: Always.
        """
        raise GrantDeniedError("run-1:root:3", "the grant list has no network policy")

    calls = _FakeCalls(["```sh\nls\n```", _file("answer.txt")])
    proposer = gepa_mod._LoopRepoProposer.bind(calls.ctx(tmp_path, run_command=denied), Task(""))
    assert proposer is not None

    with pytest.raises(GrantDeniedError):
        proposer({"c": "tree-parent"}, {"c": []}, ["c"])
    assert isinstance(proposer.failure, GrantDeniedError)
    assert calls.writes == []


def test_a_box_failure_in_a_proposer_command_reaches_the_model_and_the_proposal_goes_on(tmp_path: Path) -> None:
    """A box that failed is the command's own failure, so the model gets a note and its next reply still runs.

    Args:
        tmp_path: Pytest's temporary folder.
    """

    def failed(tree_id: str, command: str, grants: tuple[Any, ...]) -> tuple[CommandResult, str]:
        """Fail the box, as the loop does for a box that died.

        Args:
            tree_id: Not used.
            command: Not used.
            grants: Not used.

        Raises:
            BoxFailureError: Always.
        """
        raise BoxFailureError("run-1:root:3", "the box died")

    calls = _FakeCalls(["```sh\nls\n```", _file("answer.txt")])
    proposer = gepa_mod._LoopRepoProposer.bind(calls.ctx(tmp_path, run_command=failed), Task(""))
    assert proposer is not None

    assert proposer({"c": "tree-parent"}, {"c": []}, ["c"]) == {"c": "tree-1"}
    assert calls.prompts[1] == "The command did not run: Step run-1:root:3: the box died"
    assert proposer.failure is None


def test_a_hidden_path_inside_the_editable_paths_gets_the_generic_note_and_no_write(tmp_path: Path) -> None:
    """When the whole repository is editable, a hidden path gets only the generic note and is not written.

    Args:
        tmp_path: Pytest's temporary folder.
    """
    calls = _FakeCalls([_file("eval/metric.py")] + ["no block"] * (gepa_mod._MAX_TURNS - 1))
    seed = RepoSeed("tree-seed", editable_paths=(".",), hidden_hashes=hidden_hashes(["eval/metric.py"]))
    proposer = gepa_mod._LoopRepoProposer.bind(calls.ctx(tmp_path, repo_seed=seed), Task(""))
    assert proposer is not None

    assert proposer({"c": "tree-parent"}, {"c": []}, ["c"]) == {"c": "tree-parent"}
    assert calls.prompts[1] == "The files break the editing rules: 'eval/metric.py' may not be changed."
    assert calls.writes == []


def test_a_stopped_proposer_raises_the_stop_again_with_no_model_call(tmp_path: Path) -> None:
    """After a stop, a later proposal raises the kept stop before it calls the model.

    Args:
        tmp_path: Pytest's temporary folder.
    """
    model_calls: list[int] = []

    def stopped(prompt: Any, tools: Any) -> ModelReply:
        """Stop, as a fenced or closed loop does.

        Args:
            prompt: Not used.
            tools: Not used.

        Raises:
            LoopStoppedError: Always.
        """
        model_calls.append(len(prompt))
        raise LoopStoppedError("Run run-1 is closed.")

    proposer = gepa_mod._LoopRepoProposer.bind(_FakeCalls([]).ctx(tmp_path, call_model=stopped), Task(""))
    assert proposer is not None
    with pytest.raises(LoopStoppedError) as first:
        proposer({"c": "tree-parent"}, {"c": []}, ["c"])

    with pytest.raises(LoopStoppedError) as second:
        proposer({"c": "tree-parent"}, {"c": []}, ["c"])
    assert second.value is first.value
    assert model_calls == [1]
