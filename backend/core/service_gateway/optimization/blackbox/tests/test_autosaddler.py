"""Tests for the AutoSaddler engine in the worker engine loop (SKY-723).

The engine drives a child runner. These tests give it a fake runner that uses
the real runner's channel and tool loop, but not the upstream package, which
the backend venv does not carry. The loop uses a SQLite file, local subprocess
boxes and a fake model: no network and no paid call.
"""

from __future__ import annotations

import json
import shlex
import signal
import subprocess
import sys
import threading
from collections.abc import Iterator
from decimal import Decimal
from pathlib import Path
from types import SimpleNamespace
from typing import Any

import pytest
from sqlalchemy import create_engine, select, update
from sqlalchemy.engine import Engine
from sqlalchemy.orm import Session

from core.billing.budgets import BudgetService
from core.exceptions import ServiceError
from core.service_gateway.optimization.blackbox import autosaddler, autosaddler_runner
from core.service_gateway.optimization.blackbox.autosaddler import AutoSaddlerEngine
from core.service_gateway.optimization.blackbox.harness_bridge import hidden_hashes
from core.service_gateway.optimization.blackbox.protocol import (
    EngineContext,
    EvalServer,
    ModelReply,
    RepoSeed,
    Result,
    Task,
)
from core.service_gateway.optimization.blackbox.sandbox import LocalSubprocessRuntime, SandboxSpec
from core.service_gateway.optimization.blackbox.tree_sandbox import RuntimeBoxSource
from core.storage.models import AgentStepModel, Base, BillingCustomerModel, JobModel
from core.storage.object_store import LocalObjectStore
from core.storage.step_log_store import StepLogStore
from core.storage.tree_store import TreeEntry, TreeStore
from core.storage.workspace_store import WorkspaceStore
from core.worker.actors import User
from core.worker.engine_loop import ROOT_AGENT_ID, EngineLoop

from .mocks import make_ctx

RUN_ID = "run-1"
_SPEC = SandboxSpec(lifetime_seconds=60, allowed_hosts=(), operation_key="op")
_METRIC = "wc -c < outputs/answer.txt"
# The session's steps, then the scorer's two box commands.
_KINDS = ["model", "model", "command", "model", "command", "command"]

# A stand-in for the runner: the real channel and tool loop, with the upstream outcome types made plain. The
# task prompt names the process folder, which differs in a resume, so a resume matches only through ``scrub``.
_SESSION_RUNNER = """
import importlib.util, json, sys
from pathlib import Path

spec = importlib.util.spec_from_file_location("autosaddler_runner", {runner!r})
runner = importlib.util.module_from_spec(spec)
spec.loader.exec_module(runner)
runner.SessionUsage = lambda **fields: fields
runner.TransportOutcome = lambda raw_response, usage: {{"text": raw_response, "usage": usage}}
assert sys.argv[1:] == [runner.WORKER_FLAG]

channel = runner.open_channel()
runner.seed_ids()
payload = json.loads(sys.stdin.readline())
root = Path.cwd().resolve()
workspace = root / "session"
workspace.mkdir()
transport = runner.StepTransport(channel, root, None, None)
seed = payload["task"]["seed_candidate"]
try:
    outcome = transport.converse(workspace, f"improve {{seed}} in {{root}}")
    score, info = channel.evaluate(outcome["text"])
    result = {{"best_candidate": outcome["text"], "best_score": score, "metadata": {{"usage": outcome["usage"][0]}}}}
except runner.BudgetStopped:
    result = {{"best_candidate": seed, "best_score": None, "stop_reason": "budget_reached"}}
channel.writer.write(json.dumps({{"op": "result", "result": result}}) + "\\n")
channel.writer.flush()
"""

# A stand-in that checks out the seed tree as the real runner does, and also asks for one hidden file by name.
_CHECKOUT_RUNNER = """
import importlib.util, json, sys
from pathlib import Path

spec = importlib.util.spec_from_file_location("autosaddler_runner", {runner!r})
runner = importlib.util.module_from_spec(spec)
spec.loader.exec_module(runner)

channel = runner.open_channel()
payload = json.loads(sys.stdin.readline())
tree_id = payload["repo"]["tree_id"]
checkout = Path.cwd() / "checkout"
runner.checkout_seed(channel, tree_id, checkout)
seen = sorted(path.relative_to(checkout).as_posix() for path in checkout.rglob("*") if ".git" not in path.parts)
metadata = {{
    "seen": seen,
    "read": channel.read_file(tree_id, "eval/metric.py"),
    "listed": channel.list_files(tree_id, "eval"),
}}
result = {{"best_candidate": payload["task"]["seed_candidate"], "best_score": None, "metadata": metadata}}
channel.writer.write(json.dumps({{"op": "result", "result": result}}) + "\\n")
channel.writer.flush()
"""


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


class _Model:
    """A fake model with a fixed session: write the answer, check it in a box, then give it."""

    def __init__(self, die_at: int | None = None) -> None:
        """Set the call in which the worker dies.

        Args:
            die_at: The index of the call in which the worker dies; ``None`` never dies.
        """
        self.die_at = die_at
        self.calls = 0
        self.prompts: list[Any] = []
        self.tools: list[Any] = []
        self.lock = threading.Lock()

    def __call__(self, prompt: Any, tools: list[dict[str, Any]] | None) -> ModelReply:
        """Answer by the session's turn, which the number of messages gives.

        Args:
            prompt: The session's messages.
            tools: The tool definitions.

        Returns:
            A tool call, or the final text.

        Raises:
            _WorkerDiedError: In the call at ``die_at``.
        """
        with self.lock:
            index = self.calls
            self.calls += 1
        if index == self.die_at:
            raise _WorkerDiedError
        self.prompts.append(prompt)
        self.tools.append(tools)
        turn = len(prompt)
        if turn == 2:
            arguments = json.dumps({"path": "answer.txt", "content": "seedx"})
            return ModelReply("", Decimal(1), [_call("c1", "write_file", arguments)])
        if turn == 4:
            return ModelReply("", Decimal(1), [_call("c2", "run_command", json.dumps({"command": "cat answer.txt"}))])
        return ModelReply("seedx", Decimal(1), [])


def _call(call_id: str, name: str, arguments: str) -> dict[str, Any]:
    """Build one tool call in the OpenAI shape.

    Args:
        call_id: The call id.
        name: The tool name.
        arguments: The arguments as JSON text.

    Returns:
        The tool call.
    """
    return {"id": call_id, "type": "function", "function": {"name": name, "arguments": arguments}}


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
    hidden: dict[str, str] | None = None,
) -> tuple[EngineLoop, _Model]:
    """Start the run at a generation and build a loop over local boxes and the fake model.

    Args:
        owner: Alice and her budget generation.
        engine: The database.
        step_log: The step log.
        trees: The tree store.
        tmp_path: Pytest's temporary folder.
        generation: The job's execution generation.
        die_at: The model call in which the worker dies.
        hidden: The run's hidden-path hashes.

    Returns:
        The loop and its model.
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
    model = _Model(die_at)
    loop = EngineLoop(
        run,
        engine=engine,
        trees=trees,
        candidates=RuntimeBoxSource(LocalSubprocessRuntime(), _SPEC),
        metrics=RuntimeBoxSource(LocalSubprocessRuntime(), _SPEC),
        model=model,
        model_max_cents=2,
        run_dir=tmp_path / f"loop-{generation}",
        hidden_hashes=hidden,
    )
    return loop, model


def _steps(engine: Engine) -> list[tuple[str, int]]:
    """Return the root agent's committed steps.

    Args:
        engine: The database.

    Returns:
        ``(kind, execution_generation)`` for each step, in order.
    """
    with Session(engine) as session:
        rows = session.execute(
            select(AgentStepModel.kind, AgentStepModel.execution_generation)
            .where(AgentStepModel.run_id == RUN_ID, AgentStepModel.agent_id == ROOT_AGENT_ID)
            .order_by(AgentStepModel.seq)
        )
        return [(row.kind, row.execution_generation) for row in rows]


def _runner(tmp_path: Path, source: str) -> AutoSaddlerEngine:
    """Write a stand-in runner and return an engine that starts it with this interpreter.

    Args:
        tmp_path: Pytest's temporary folder.
        source: The runner's Python source.

    Returns:
        The engine.
    """
    script = tmp_path / "fake_runner.py"
    script.write_text(source, encoding="utf-8")
    return AutoSaddlerEngine(python=sys.executable, runner=script)


def _session_engine(tmp_path: Path) -> AutoSaddlerEngine:
    """Return an engine whose child runs one session over the real runner's channel and tool loop.

    Args:
        tmp_path: Pytest's temporary folder.

    Returns:
        The engine.
    """
    return _runner(tmp_path, _SESSION_RUNNER.format(runner=str(autosaddler.RUNNER)))


def _record_children(monkeypatch: pytest.MonkeyPatch) -> list[subprocess.Popen[str]]:
    """Record each child process that the engine starts.

    Args:
        monkeypatch: The pytest patcher; it replaces the engine module's ``subprocess`` only.

    Returns:
        The list that gets each child.
    """
    children: list[subprocess.Popen[str]] = []

    def popen(*args: Any, **kwargs: Any) -> subprocess.Popen[str]:
        """Start the child and record it.

        Args:
            *args: The ``Popen`` arguments.
            **kwargs: The ``Popen`` keyword arguments.

        Returns:
            The child.
        """
        children.append(subprocess.Popen(*args, **kwargs))
        return children[-1]

    monkeypatch.setattr(autosaddler, "subprocess", SimpleNamespace(**{**vars(subprocess), "Popen": popen}))
    return children


def _no_native_box(*_: Any) -> Any:
    """Fail the test: the loop path must not ship the run to a native box.

    Raises:
        AssertionError: Always.
    """
    raise AssertionError("The loop path called run_native_engine.")


def test_the_loop_path_never_calls_the_native_box(
    owner: tuple[User, int],
    engine: Engine,
    step_log: StepLogStore,
    trees: TreeStore,
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """A context with the loop's calls runs the child in the worker, and never the native box.

    Args:
        owner: Alice and her budget generation.
        engine: The database.
        step_log: The step log.
        trees: The tree store.
        tmp_path: Pytest's temporary folder.
        monkeypatch: Makes the native box fail the test.
    """
    monkeypatch.setattr(autosaddler, "run_native_engine", _no_native_box)
    loop, _ = _loop(owner, engine, step_log, trees, tmp_path, generation=1)
    scorer = _scorer(_tree(trees, "README", "seed"), _tree(trees, "metric.txt", "len"))

    result = _session_engine(tmp_path).run(Task("seed"), loop.server(scorer, max_evals=4), loop.context())

    assert result.best_candidate == "seedx"


def test_a_context_with_no_worker_host_runs_the_native_box_once(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Until the SKY-728 cutover, a context with no model, command or tree call runs the native box once.

    Args:
        tmp_path: Pytest's temporary folder.
        monkeypatch: Replaces the native box with a recorder.
    """
    calls: list[tuple[str, Task, EvalServer, EngineContext]] = []
    expected = Result(best_candidate="native", best_score=1.0, total_evals=1)

    def native(engine_id: str, task: Task, server: EvalServer, ctx: EngineContext) -> Result:
        """Record the call and return a native result.

        Args:
            engine_id: The engine's name.
            task: The task.
            server: The eval server.
            ctx: The context.

        Returns:
            The native result.
        """
        calls.append((engine_id, task, server, ctx))
        return expected

    monkeypatch.setattr(autosaddler, "run_native_engine", native)
    task, server, ctx = Task("seed"), EvalServer(lambda *_: (1.0, {}), max_evals=2), make_ctx(str(tmp_path))

    assert AutoSaddlerEngine().run(task, server, ctx) is expected
    assert calls == [("autosaddler", task, server, ctx)]


def test_the_worker_flag_matches_the_runner() -> None:
    """The engine starts the runner with the flag that selects the runner's worker mode."""
    assert autosaddler.WORKER_FLAG == autosaddler_runner.WORKER_FLAG


def test_a_hidden_path_is_not_visible_to_the_engine(
    owner: tuple[User, int], engine: Engine, step_log: StepLogStore, trees: TreeStore, tmp_path: Path
) -> None:
    """The child's seed checkout, listing and read leave out the seed's hidden paths.

    Args:
        owner: Alice and her budget generation.
        engine: The database.
        step_log: The step log.
        trees: The tree store.
        tmp_path: Pytest's temporary folder.
    """
    hidden = hidden_hashes(["eval", "optimize.toml"])
    loop, _ = _loop(owner, engine, step_log, trees, tmp_path, generation=1, hidden=hidden)
    tree = loop.put_files({"eval/metric.py": b"secret", "optimize.toml": b"secret", "src/a.py": b"a"})
    seed = RepoSeed(tree, editable_paths=("src",), hidden_hashes=hidden)
    child = _runner(tmp_path, _CHECKOUT_RUNNER.format(runner=str(autosaddler.RUNNER)))

    result = child.run(Task(""), loop.server(lambda *_: (1.0, {}), max_evals=1), loop.context(repo_seed=seed))

    assert result.metadata["seen"] == ["src", "src/a.py"]
    assert result.metadata["read"] is None
    assert result.metadata["listed"] is None


def test_each_model_reply_and_command_is_one_step(
    owner: tuple[User, int], engine: Engine, step_log: StepLogStore, trees: TreeStore, tmp_path: Path
) -> None:
    """Run a session: one step for each model reply and each command, and the scorer goes through the loop.

    Args:
        owner: Alice and her budget generation.
        engine: The database.
        step_log: The step log.
        trees: The tree store.
        tmp_path: Pytest's temporary folder.
    """
    loop, model = _loop(owner, engine, step_log, trees, tmp_path, generation=1)
    scorer = _scorer(_tree(trees, "README", "seed"), _tree(trees, "metric.txt", "len"))

    result = _session_engine(tmp_path).run(Task("seed"), loop.server(scorer, max_evals=4), loop.context())

    assert (result.best_candidate, result.best_score, result.total_evals) == ("seedx", 5.0, 1)
    assert result.metadata["upstream_revision"] == autosaddler.AUTOSADDLER_REVISION
    assert result.metadata["usage"]["provider_cost"] == 0.03
    assert [kind for kind, _ in _steps(engine)] == _KINDS
    assert model.tools == [autosaddler_runner._TOOLS] * 3
    # The model sees the box's output, and no worker folder.
    assert model.prompts[-1][-1]["content"].startswith("exit code 0\nstdout:\nseedx")
    assert model.prompts[0][1]["content"] == "improve seed in /work"


def test_a_killed_run_resumes_from_its_last_step(
    owner: tuple[User, int],
    engine: Engine,
    step_log: StepLogStore,
    trees: TreeStore,
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Kill the worker in the third model call, then resume on a new generation from the committed steps.

    Args:
        owner: Alice and her budget generation.
        engine: The database.
        step_log: The step log.
        trees: The tree store.
        tmp_path: Pytest's temporary folder.
        monkeypatch: Records the child.
    """
    seed_tree, metric_tree = _tree(trees, "README", "seed"), _tree(trees, "metric.txt", "len")
    first, _ = _loop(owner, engine, step_log, trees, tmp_path, generation=1, die_at=2)
    children = _record_children(monkeypatch)
    with pytest.raises(_WorkerDiedError):
        _session_engine(tmp_path).run(
            Task("seed"), first.server(_scorer(seed_tree, metric_tree), max_evals=4), first.context()
        )
    assert [kind for kind, _ in _steps(engine)] == _KINDS[:3]
    # The worker's death stops the child too.
    assert children[0].poll() is not None

    with Session(engine) as session:
        session.execute(update(JobModel).where(JobModel.optimization_id == RUN_ID).values(execution_generation=2))
        session.commit()
    loop, model = _loop(owner, engine, step_log, trees, tmp_path, generation=2)
    result = _session_engine(tmp_path).run(
        Task("seed"), loop.server(_scorer(seed_tree, metric_tree), max_evals=4), loop.context()
    )

    assert (result.best_candidate, result.best_score) == ("seedx", 5.0)
    # The two committed replies come back from the log; only the call that never committed runs again.
    assert model.calls == 1
    assert _steps(engine) == [(kind, 1) for kind in _KINDS[:3]] + [(kind, 2) for kind in _KINDS[3:]]


def test_a_spent_budget_stops_the_child_with_its_seed(
    owner: tuple[User, int], engine: Engine, step_log: StepLogStore, trees: TreeStore, tmp_path: Path
) -> None:
    """With no evaluation left, the child hears a budget stop and the run keeps its seed.

    Args:
        owner: Alice and her budget generation.
        engine: The database.
        step_log: The step log.
        trees: The tree store.
        tmp_path: Pytest's temporary folder.
    """
    loop, _ = _loop(owner, engine, step_log, trees, tmp_path, generation=1)
    scorer = _scorer(_tree(trees, "README", "seed"), _tree(trees, "metric.txt", "len"))

    result = _session_engine(tmp_path).run(Task("seed"), loop.server(scorer, max_evals=0), loop.context())

    assert (result.best_candidate, result.best_score, result.total_evals) == ("seed", None, 0)
    assert result.metadata["stop_reason"] == "budget_reached"


def test_a_failure_envelope_is_a_service_error(
    owner: tuple[User, int], engine: Engine, step_log: StepLogStore, trees: TreeStore, tmp_path: Path
) -> None:
    """The child's error reaches the job as a service error.

    Args:
        owner: Alice and her budget generation.
        engine: The database.
        step_log: The step log.
        trees: The tree store.
        tmp_path: Pytest's temporary folder.
    """
    loop, _ = _loop(owner, engine, step_log, trees, tmp_path, generation=1)
    script = (
        "import sys, json\nsys.stdin.readline()\n"
        'print(json.dumps({"op": "result", "result": {"error": "RuntimeError: boom"}}), flush=True)\n'
    )

    with pytest.raises(ServiceError, match="boom"):
        _runner(tmp_path, script).run(Task("seed"), loop.server(lambda *_: (1.0, {}), max_evals=1), loop.context())


@pytest.mark.parametrize(
    ("script", "message"),
    [
        (
            'import sys\nsys.stdin.readline()\nprint(\'{"op": "shell"}\', flush=True)\nsys.stdin.readline()\n',
            "unknown request",
        ),
        ("import sys\nsys.stdin.readline()\n", "exited with no result"),
    ],
)
def test_a_child_that_breaks_the_protocol_is_stopped(
    owner: tuple[User, int],
    engine: Engine,
    step_log: StepLogStore,
    trees: TreeStore,
    tmp_path: Path,
    script: str,
    message: str,
) -> None:
    """An unknown request or an exit with no result fails the run with no step.

    Args:
        owner: Alice and her budget generation.
        engine: The database.
        step_log: The step log.
        trees: The tree store.
        tmp_path: Pytest's temporary folder.
        script: The child's source.
        message: The expected error text.
    """
    loop, _ = _loop(owner, engine, step_log, trees, tmp_path, generation=1)

    with pytest.raises(ServiceError, match=message):
        _runner(tmp_path, script).run(Task("seed"), loop.server(lambda *_: (1.0, {}), max_evals=1), loop.context())

    assert not _steps(engine)


def test_the_child_gets_only_its_four_environment_variables(
    owner: tuple[User, int], engine: Engine, step_log: StepLogStore, trees: TreeStore, tmp_path: Path
) -> None:
    """The child gets no worker secret: its environment holds PATH, HOME, LANG and PYTHONHASHSEED only.

    Args:
        owner: Alice and her budget generation.
        engine: The database.
        step_log: The step log.
        trees: The tree store.
        tmp_path: Pytest's temporary folder.
    """
    loop, _ = _loop(owner, engine, step_log, trees, tmp_path, generation=1)
    script = (
        "import json, os, sys\nsys.stdin.readline()\n"
        'result = {"best_candidate": "seed", "best_score": 1.0, "metadata": {"env": sorted(os.environ)}}\n'
        'print(json.dumps({"op": "result", "result": result}), flush=True)\n'
    )

    result = _runner(tmp_path, script).run(Task("seed"), loop.server(lambda *_: (1.0, {}), max_evals=1), loop.context())

    assert result.metadata["env"] == ["HOME", "LANG", "PATH", "PYTHONHASHSEED"]


def test_a_candidate_with_the_wrong_shape_is_never_scored(
    owner: tuple[User, int], engine: Engine, step_log: StepLogStore, trees: TreeStore, tmp_path: Path
) -> None:
    """A text task's evaluation request with a named-parts candidate fails the run before any budget is spent.

    Args:
        owner: Alice and her budget generation.
        engine: The database.
        step_log: The step log.
        trees: The tree store.
        tmp_path: Pytest's temporary folder.
    """
    loop, _ = _loop(owner, engine, step_log, trees, tmp_path, generation=1)
    scored: list[Any] = []
    server = loop.server(lambda _loop, candidate, _case: (scored.append(candidate), (1.0, {}))[1], max_evals=1)
    script = (
        "import sys\nsys.stdin.readline()\n"
        'print(\'{"op": "evaluate", "candidate": {"a": "b"}, "example": null}\', flush=True)\nsys.stdin.readline()\n'
    )

    with pytest.raises(ServiceError, match="wrong shape"):
        _runner(tmp_path, script).run(Task("seed"), server, loop.context())

    assert scored == []
    assert server.used == 0


def test_a_version_event_with_a_bad_candidate_is_dropped(
    owner: tuple[User, int], engine: Engine, step_log: StepLogStore, trees: TreeStore, tmp_path: Path
) -> None:
    """The run view gets the version events whose candidate has the task's shape, and no other.

    Args:
        owner: Alice and her budget generation.
        engine: The database.
        step_log: The step log.
        trees: The tree store.
        tmp_path: Pytest's temporary folder.
    """
    loop, _ = _loop(owner, engine, step_log, trees, tmp_path, generation=1)
    events: list[tuple[str, dict[str, Any]]] = []
    lines = [
        {"op": "progress", "event": {"candidate_id": 0, "score": 1.0, "per_example": []}},
        {"op": "progress", "event": {"candidate_id": 1, "score": 1.0, "per_example": [], "candidate": 5}},
        {"op": "progress", "event": {"candidate_id": 2, "score": 1.0, "per_example": [], "candidate": "good"}},
        {"op": "result", "result": {"best_candidate": "good", "best_score": 1.0}},
    ]
    script = "import sys\nsys.stdin.readline()\n" + "".join(
        f"print({json.dumps(line)!r}, flush=True)\nsys.stdin.readline()\n" for line in lines
    )

    _runner(tmp_path, script).run(
        Task("seed"),
        loop.server(lambda *_: (1.0, {}), max_evals=1),
        loop.context(progress_callback=lambda kind, payload: events.append((kind, payload))),
    )

    assert [kind for kind, _ in events] == ["candidate"]
    assert "good" in json.dumps(events[0][1])


def test_a_best_candidate_with_the_wrong_shape_is_a_service_error(
    owner: tuple[User, int], engine: Engine, step_log: StepLogStore, trees: TreeStore, tmp_path: Path
) -> None:
    """A result whose best candidate is not text, for a text task, fails the run.

    Args:
        owner: Alice and her budget generation.
        engine: The database.
        step_log: The step log.
        trees: The tree store.
        tmp_path: Pytest's temporary folder.
    """
    loop, _ = _loop(owner, engine, step_log, trees, tmp_path, generation=1)
    script = (
        "import sys\nsys.stdin.readline()\n"
        'print(\'{"op": "result", "result": {"best_candidate": 5, "best_score": 1.0}}\', flush=True)\n'
    )

    with pytest.raises(ServiceError, match="best candidate with the wrong shape"):
        _runner(tmp_path, script).run(Task("seed"), loop.server(lambda *_: (1.0, {}), max_evals=1), loop.context())


@pytest.mark.parametrize(
    ("script", "message"),
    [
        ("import sys\nsys.stdin.readline()\nprint('not json', flush=True)\nsys.stdin.readline()\n", "not a JSON"),
        ("import sys\nsys.stdin.readline()\nprint('[1]', flush=True)\nsys.stdin.readline()\n", "not a JSON"),
        # The child closes its stdin and stays alive, so the host's reply meets a broken pipe.
        (
            "import os, sys, time\nsys.stdin.readline()\nos.close(0)\n"
            'print(\'{"op": "progress", "event": {}}\', flush=True)\ntime.sleep(30)\n',
            "exited with no result",
        ),
        ("import sys, time\nsys.stdin.readline()\ntime.sleep(30)\n", "sent no request for 0.5 seconds"),
        # A request with a missing field, a field of the wrong type, or a tree the run can not see.
        *(
            (f"import sys\nsys.stdin.readline()\nprint({line!r}, flush=True)\nsys.stdin.readline()\n", "bad request")
            for line in (
                '{"op": "read_file", "path": "a"}',
                '{"op": "list_files"}',
                '{"op": "model", "tools": null}',
                '{"op": "command", "command": "true"}',
                '{"op": "put_files", "files": [1]}',
                '{"op": "read_file", "tree_id": "missing", "path": "a"}',
            )
        ),
    ],
)
def test_a_broken_or_silent_child_is_a_service_error_and_is_stopped(
    owner: tuple[User, int],
    engine: Engine,
    step_log: StepLogStore,
    trees: TreeStore,
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    script: str,
    message: str,
) -> None:
    """A bad request line or field, a closed stdin or a long silence fails the run with a pointer to the log.

    Args:
        owner: Alice and her budget generation.
        engine: The database.
        step_log: The step log.
        trees: The tree store.
        tmp_path: Pytest's temporary folder.
        monkeypatch: Shortens the silence limit and records the child.
        script: The child's source.
        message: The expected error text.
    """
    monkeypatch.setattr(autosaddler, "_IDLE_SECONDS", 0.5)
    children = _record_children(monkeypatch)
    loop, _ = _loop(owner, engine, step_log, trees, tmp_path, generation=1)

    with pytest.raises(ServiceError, match=message):
        _runner(tmp_path, script).run(Task("seed"), loop.server(lambda *_: (1.0, {}), max_evals=1), loop.context())

    assert children[0].poll() is not None


def test_a_child_that_does_not_exit_after_its_result_is_stopped(
    owner: tuple[User, int],
    engine: Engine,
    step_log: StepLogStore,
    trees: TreeStore,
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """The host keeps the result of a child that hangs after it, and kills the child.

    Args:
        owner: Alice and her budget generation.
        engine: The database.
        step_log: The step log.
        trees: The tree store.
        tmp_path: Pytest's temporary folder.
        monkeypatch: Shortens the exit limit and records the child.
    """
    monkeypatch.setattr(autosaddler, "_EXIT_SECONDS", 0.5)
    children = _record_children(monkeypatch)
    loop, _ = _loop(owner, engine, step_log, trees, tmp_path, generation=1)
    script = (
        "import sys, time\nsys.stdin.readline()\n"
        'print(\'{"op": "result", "result": {"best_candidate": "seed", "best_score": 1.0}}\', flush=True)\n'
        "time.sleep(30)\n"
    )

    result = _runner(tmp_path, script).run(Task("seed"), loop.server(lambda *_: (1.0, {}), max_evals=1), loop.context())

    assert result.best_candidate == "seed"
    assert children[0].returncode == -signal.SIGKILL
