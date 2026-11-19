"""Tests for the AutoSaddler engine in the worker engine loop (SKY-723).

The engine drives a child runner. These tests give it a fake runner that uses
the real runner's channel and tool loop, but not the upstream package, which
the backend venv does not carry. The loop uses a SQLite file, local subprocess
boxes and a fake model: no network and no paid call.
"""

from __future__ import annotations

import json
import math
import os
import shlex
import signal
import subprocess
import sys
import threading
from collections.abc import Iterator
from dataclasses import replace
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
    BudgetExhaustedError,
    EngineContext,
    EvalServer,
    ModelReply,
    RepoSeed,
    Task,
    ToolCall,
)
from core.service_gateway.optimization.blackbox.sandbox import LocalSubprocessRuntime, SandboxSpec
from core.service_gateway.optimization.blackbox.tree_sandbox import RuntimeBoxSource
from core.storage.models import AgentStepModel, Base, BillingCustomerModel, JobModel
from core.storage.object_store import LocalObjectStore
from core.storage.step_log_store import StepLogStore
from core.storage.tree_store import TreeEntry, TreeStore
from core.storage.workspace_store import WorkspaceStore
from core.worker.actors import User
from core.worker.engine_loop import ROOT_AGENT_ID, EngineLoop, ModelCallFailedError

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
            return ModelReply("", Decimal(1), [_call("c1", "write_file", {"path": "answer.txt", "content": "seedx"})])
        if turn == 4:
            return ModelReply("", Decimal(1), [_call("c2", "run_command", {"command": "cat answer.txt"})])
        return ModelReply("seedx", Decimal(1), [])


def _call(call_id: str, name: str, arguments: dict[str, Any]) -> ToolCall:
    """Build one tool call in the engine loop's flat shape.

    Args:
        call_id: The call id.
        name: The tool name.
        arguments: The arguments.

    Returns:
        The tool call.
    """
    return {"id": call_id, "name": name, "arguments": arguments}


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
    run_dir: Path | None = None,
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
        run_dir: The loop's run folder; a new folder for each generation when not given.

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
        run_dir=run_dir or tmp_path / f"loop-{generation}",
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


def test_the_loop_path_runs_the_child_in_the_worker(
    owner: tuple[User, int],
    engine: Engine,
    step_log: StepLogStore,
    trees: TreeStore,
    tmp_path: Path,
) -> None:
    """A context with the loop's calls runs the child in the worker.

    Args:
        owner: Alice and her budget generation.
        engine: The database.
        step_log: The step log.
        trees: The tree store.
        tmp_path: Pytest's temporary folder.
    """
    loop, _ = _loop(owner, engine, step_log, trees, tmp_path, generation=1)
    scorer = _scorer(_tree(trees, "README", "seed"), _tree(trees, "metric.txt", "len"))

    result = _session_engine(tmp_path).run(Task("seed"), loop.server(scorer, max_evals=4), loop.context())

    assert result.best_candidate == "seedx"


def test_a_context_with_no_worker_host_is_refused(tmp_path: Path) -> None:
    """A context with no model, command or tree call fails closed: no native box runs the engine (SKY-728).

    Args:
        tmp_path: Pytest's temporary folder.
    """
    task, server, ctx = Task("seed"), EvalServer(lambda *_: (1.0, {}), max_evals=2), make_ctx(str(tmp_path))

    with pytest.raises(ServiceError, match="only on the worker engine loop"):
        AutoSaddlerEngine().run(task, server, ctx)


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
    # put_files refuses a hidden path, so the test stores the seed straight in the tree store, as the run does.
    tree = trees.put_tree(
        RUN_ID,
        [
            TreeEntry("eval", "tree", _tree(trees, "metric.py", "secret")),
            TreeEntry("optimize.toml", "file", trees.put_blob(RUN_ID, b"secret")),
            TreeEntry("src", "tree", _tree(trees, "a.py", "a")),
        ],
    )
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


def test_a_resume_in_the_same_run_folder_replays_from_its_last_step(
    owner: tuple[User, int],
    engine: Engine,
    step_log: StepLogStore,
    trees: TreeStore,
    tmp_path: Path,
) -> None:
    """A resume that uses the killed run's folder clears the old files and replays the committed steps.

    Args:
        owner: Alice and her budget generation.
        engine: The database.
        step_log: The step log.
        trees: The tree store.
        tmp_path: Pytest's temporary folder.
    """
    seed_tree, metric_tree = _tree(trees, "README", "seed"), _tree(trees, "metric.txt", "len")
    run_dir = tmp_path / "loop"
    first, _ = _loop(owner, engine, step_log, trees, tmp_path, generation=1, die_at=2, run_dir=run_dir)
    with pytest.raises(_WorkerDiedError):
        _session_engine(tmp_path).run(
            Task("seed"), first.server(_scorer(seed_tree, metric_tree), max_evals=4), first.context()
        )
    assert (run_dir / "autosaddler").is_dir()

    with Session(engine) as session:
        session.execute(update(JobModel).where(JobModel.optimization_id == RUN_ID).values(execution_generation=2))
        session.commit()
    loop, model = _loop(owner, engine, step_log, trees, tmp_path, generation=2, run_dir=run_dir)
    result = _session_engine(tmp_path).run(
        Task("seed"), loop.server(_scorer(seed_tree, metric_tree), max_evals=4), loop.context()
    )

    assert (result.best_candidate, result.best_score) == ("seedx", 5.0)
    assert model.calls == 1
    assert _steps(engine) == [(kind, 1) for kind in _KINDS[:3]] + [(kind, 2) for kind in _KINDS[3:]]


def test_a_spent_budget_fails_before_the_child_starts(
    owner: tuple[User, int],
    engine: Engine,
    step_log: StepLogStore,
    trees: TreeStore,
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """With no evaluation left, the run stops as a spent budget and starts no child, as the native box does.

    Args:
        owner: Alice and her budget generation.
        engine: The database.
        step_log: The step log.
        trees: The tree store.
        tmp_path: Pytest's temporary folder.
        monkeypatch: Records the child.
    """
    children = _record_children(monkeypatch)
    loop, _ = _loop(owner, engine, step_log, trees, tmp_path, generation=1)
    scorer = _scorer(_tree(trees, "README", "seed"), _tree(trees, "metric.txt", "len"))

    with pytest.raises(BudgetExhaustedError):
        _session_engine(tmp_path).run(Task("seed"), loop.server(scorer, max_evals=0), loop.context())

    assert not children
    assert not _steps(engine)


def test_an_evaluation_past_the_budget_gets_a_budget_stop(
    owner: tuple[User, int], engine: Engine, step_log: StepLogStore, trees: TreeStore, tmp_path: Path
) -> None:
    """The child's evaluation after the last one gets a budget stop, and the run keeps its result.

    Args:
        owner: Alice and her budget generation.
        engine: The database.
        step_log: The step log.
        trees: The tree store.
        tmp_path: Pytest's temporary folder.
    """
    loop, _ = _loop(owner, engine, step_log, trees, tmp_path, generation=1)
    request = json.dumps({"op": "evaluate", "candidate": "seed", "example": None, "candidate_id": 0})
    script = (
        "import json, sys\nsys.stdin.readline()\nreplies = []\n"
        + f"print({request!r}, flush=True)\nreplies.append(json.loads(sys.stdin.readline()))\n" * 2
        + 'result = {"best_candidate": "seed", "best_score": 1.0, "metadata": {"replies": replies}}\n'
        'print(json.dumps({"op": "result", "result": result}), flush=True)\n'
    )

    result = _runner(tmp_path, script).run(Task("seed"), loop.server(lambda *_: (1.0, {}), max_evals=1), loop.context())

    first, second = result.metadata["replies"]
    assert first["score"] == 1.0
    assert second["stop_reason"] == "budget_reached"


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

    HOME is the child's own working folder, so a tool that writes under HOME stays in the run folder.

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
        'metadata = {"env": sorted(os.environ), "home": os.environ["HOME"], "cwd": os.getcwd()}\n'
        'result = {"best_candidate": "seed", "best_score": 1.0, "metadata": metadata}\n'
        'print(json.dumps({"op": "result", "result": result}), flush=True)\n'
    )

    result = _runner(tmp_path, script).run(Task("seed"), loop.server(lambda *_: (1.0, {}), max_evals=1), loop.context())

    assert result.metadata["env"] == ["HOME", "LANG", "PATH", "PYTHONHASHSEED"]
    assert Path(result.metadata["home"]).resolve() == Path(result.metadata["cwd"]).resolve()


def test_the_child_honors_the_fixed_hash_seed_and_does_not_see_its_own_folder(tmp_path: Path) -> None:
    """The child's flags keep ``PYTHONHASHSEED``, so set order is the same in a run and its replay.

    The engine starts the image's AutoSaddler interpreter; ``APP_AUTOSADDLER_PYTHON`` names one, else this test
    uses its own interpreter with the same flags and environment.

    Args:
        tmp_path: Pytest's temporary folder.
    """
    script = (
        "import json, sys\nsys.stdin.readline()\n"
        'metadata = {"hash_randomization": sys.flags.hash_randomization, "path": sys.path}\n'
        'result = {"best_candidate": "seed", "best_score": 1.0, "metadata": metadata}\n'
        'print(json.dumps({"op": "result", "result": result}), flush=True)\n'
    )
    engine = _runner(tmp_path, script)
    engine.python = os.environ.get("APP_AUTOSADDLER_PYTHON") or sys.executable
    ctx = make_ctx(
        str(tmp_path),
        call_model=lambda *_: None,
        run_command=lambda *_: None,
        put_files=lambda *_: None,
        read_file=lambda *_: None,
        list_files=lambda *_: None,
    )

    result = engine.run(Task("seed"), EvalServer(lambda *_: (1.0, {}), max_evals=1), ctx)

    assert result.metadata["hash_randomization"] == 0
    assert str(tmp_path) not in result.metadata["path"]


def test_a_nan_in_a_reply_reaches_the_child_as_null(tmp_path: Path) -> None:
    """A NaN in a model's tool arguments or in the scorer's side information does not stop the run.

    Args:
        tmp_path: Pytest's temporary folder.
    """
    lines = [
        {"op": "model", "prompt": [{"role": "user", "content": "go"}], "tools": None},
        {"op": "evaluate", "candidate": "seed", "example": None, "candidate_id": 0},
    ]
    script = (
        "import json, sys\nsys.stdin.readline()\nreplies = []\n"
        + "".join(
            f"print({json.dumps(line)!r}, flush=True)\nreplies.append(json.loads(sys.stdin.readline()))\n"
            for line in lines
        )
        + 'result = {"best_candidate": "seed", "best_score": 1.0, "metadata": {"replies": replies}}\n'
        'print(json.dumps({"op": "result", "result": result}), flush=True)\n'
    )
    ctx = make_ctx(
        str(tmp_path),
        call_model=lambda *_: ModelReply("", Decimal(1), [_call("c1", "write_file", {"path": "a", "n": math.nan})]),
        run_command=lambda *_: None,
        put_files=lambda *_: None,
        read_file=lambda *_: None,
        list_files=lambda *_: None,
    )
    server = EvalServer(lambda *_: (1.0, {"note": math.nan, "list": [math.inf, 2.0]}), max_evals=1)

    result = _runner(tmp_path, script).run(Task("seed"), server, ctx)

    model, evaluation = result.metadata["replies"]
    assert model["tool_calls"] == [{"id": "c1", "name": "write_file", "arguments": {"path": "a", "n": None}}]
    assert evaluation == {"score": 1.0, "info": {"note": None, "list": [None, 2.0]}}


def test_a_log_record_reaches_the_run_log_with_the_context_owner(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """The child's log record goes to ``sandbox_log.forward`` with the job id that the worker's router keeps.

    Args:
        tmp_path: Pytest's temporary folder.
        monkeypatch: Records the forwarded records.
    """
    forwarded: list[tuple[Any, str | None]] = []
    monkeypatch.setattr(
        autosaddler.sandbox_log, "forward", lambda record, *, owner, scrub: forwarded.append((record, owner))
    )
    record = {"event": "rule.block", "message": "blocked"}
    script = (
        "import json, sys\nsys.stdin.readline()\n"
        f"print({json.dumps({'op': 'log', 'record': record})!r}, flush=True)\nsys.stdin.readline()\n"
        'result = {"best_candidate": "seed", "best_score": 1.0, "metadata": {}}\n'
        'print(json.dumps({"op": "result", "result": result}), flush=True)\n'
    )
    ctx = make_ctx(
        str(tmp_path),
        call_model=lambda *_: None,
        run_command=lambda *_: None,
        put_files=lambda *_: None,
        read_file=lambda *_: None,
        list_files=lambda *_: None,
        log_owner=RUN_ID,
    )

    _runner(tmp_path, script).run(Task("seed"), EvalServer(lambda *_: (1.0, {}), max_evals=1), ctx)

    assert forwarded == [(record, RUN_ID)]


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


@pytest.mark.parametrize("index", [-1, 2])
def test_a_case_index_outside_the_cases_is_never_scored(
    owner: tuple[User, int], engine: Engine, step_log: StepLogStore, trees: TreeStore, tmp_path: Path, index: int
) -> None:
    """An evaluation request that names a negative case or the case past the last fails the run unscored.

    Args:
        owner: Alice and her budget generation.
        engine: The database.
        step_log: The step log.
        trees: The tree store.
        tmp_path: Pytest's temporary folder.
        index: The case index that the child sends; the task has two cases.
    """
    loop, _ = _loop(owner, engine, step_log, trees, tmp_path, generation=1)
    scored: list[Any] = []
    server = loop.server(lambda _loop, _candidate, case: (scored.append(case), (1.0, {}))[1], max_evals=1)
    request = json.dumps({"op": "evaluate", "candidate": "seed", "example": {"id": index}})
    script = f"import sys\nsys.stdin.readline()\nprint({request!r}, flush=True)\nsys.stdin.readline()\n"

    with pytest.raises(ServiceError, match="unknown case"):
        _runner(tmp_path, script).run(Task("seed", cases=["a", "b"]), server, loop.context())

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


def _chat_script(lines: list[dict[str, Any]]) -> str:
    """Return a child that sends each line, keeps each reply, and returns the replies in its metadata.

    Args:
        lines: The requests that the child sends in order.

    Returns:
        The child's Python source.
    """
    return (
        "import json, sys\nsys.stdin.readline()\nreplies = []\n"
        + "".join(
            f"print({json.dumps(line)!r}, flush=True)\nreplies.append(json.loads(sys.stdin.readline()))\n"
            for line in lines
        )
        + 'result = {"best_candidate": "seed", "best_score": 1.0, "metadata": {"replies": replies}}\n'
        'print(json.dumps({"op": "result", "result": result}), flush=True)\n'
    )


def _bare_ctx(tmp_path: Path) -> EngineContext:
    """Return an engine context with no model, box or tree access.

    Args:
        tmp_path: Pytest's temporary folder.

    Returns:
        The context.
    """
    return make_ctx(
        str(tmp_path),
        call_model=lambda *_: None,
        run_command=lambda *_: None,
        put_files=lambda *_: None,
        read_file=lambda *_: None,
        list_files=lambda *_: None,
    )


def test_a_version_with_a_crashed_case_shows_no_score(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    """A version event for a version with a metric error reaches the run view with no score and no cases.

    The child fills a crashed case with its version's mean, so its score must not reach the run view.

    Args:
        tmp_path: Pytest's temporary folder.
        monkeypatch: Records the version events.
    """
    shown: list[dict[str, Any]] = []
    monkeypatch.setattr(autosaddler, "emit_candidate", lambda _callback, **fields: shown.append(fields))

    def scorer(_candidate: Any, case: Any) -> tuple[float, dict[str, Any]]:
        """Score case "a", and crash on case "b".

        Args:
            _candidate: The version; not used.
            case: The case.

        Returns:
            The score and side information.

        Raises:
            RuntimeError: On case "b".
        """
        if case == "b":
            raise RuntimeError("metric crashed")
        return 1.0, {}

    lines = [
        {"op": "evaluate", "candidate": "bad", "example": {"id": 0}, "candidate_id": 3},
        {"op": "evaluate", "candidate": "bad", "example": {"id": 1}, "candidate_id": 3},
        {
            "op": "progress",
            "event": {"candidate_id": 3, "score": 1.0, "candidate": "bad", "per_example": [["0", 1.0], ["1", 1.0]]},
        },
    ]

    _runner(tmp_path, _chat_script(lines)).run(
        Task("seed", cases=["a", "b"]), EvalServer(scorer, max_evals=2), _bare_ctx(tmp_path)
    )

    assert [(fields["candidate_id"], fields["score"], fields["per_example"]) for fields in shown] == [("3", None, [])]


def test_a_version_event_keeps_only_its_finite_case_scores(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    """A per-case score that is NaN or not a number is dropped, and the run goes on.

    Args:
        tmp_path: Pytest's temporary folder.
        monkeypatch: Records the version events.
    """
    shown: list[dict[str, Any]] = []
    monkeypatch.setattr(autosaddler, "emit_candidate", lambda _callback, **fields: shown.append(fields))
    per_example = [["a", 1.0], ["b", math.nan], ["c", "x"]]
    lines = [
        {"op": "progress", "event": {"candidate_id": 2, "score": 1.0, "candidate": "good", "per_example": per_example}}
    ]

    result = _runner(tmp_path, _chat_script(lines)).run(
        Task("seed"), EvalServer(lambda *_: (1.0, {}), max_evals=1), _bare_ctx(tmp_path)
    )

    assert result.best_candidate == "seed"
    assert [(fields["score"], fields["per_example"]) for fields in shown] == [(1.0, [("a", 1.0)])]


def test_scorer_feedback_needs_an_integer_candidate_id(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    """An evaluation with a null or boolean candidate id gets its score but sends no scorer feedback.

    Args:
        tmp_path: Pytest's temporary folder.
        monkeypatch: Records the scorer feedback.
    """
    feedback: list[dict[str, Any]] = []
    monkeypatch.setattr(autosaddler, "emit_scorer_feedback", lambda _callback, **fields: feedback.append(fields))
    lines = [
        {"op": "evaluate", "candidate": "seed", "example": None, "candidate_id": candidate_id}
        for candidate_id in (None, True, 0)
    ]

    result = _runner(tmp_path, _chat_script(lines)).run(
        Task("seed"), EvalServer(lambda *_: (1.0, {"note": "ok"}), max_evals=3), _bare_ctx(tmp_path)
    )

    assert result.metadata["replies"] == [{"score": 1.0, "info": {"note": "ok"}}] * 3
    assert [fields["candidate_id"] for fields in feedback] == ["0"]


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
                # Bad base64, a version count that is no number, and a result that is no object.
                '{"op": "put_files", "files": {"a.txt": "a"}}',
                '{"op": "progress", "event": {"candidate_id": 1, "score": 1.0, "candidate": "s", "total_evals": "x"}}',
                # json.loads reads 1e999 as inf, and int(inf) raises OverflowError.
                '{"op": "progress", "event": {"candidate_id": 1, "score": 1.0, "candidate": "s", "total_evals": 1e999}}',
                '{"op": "progress", "event": [1]}',
                '{"op": "progress", "event": []}',
                '{"op": "progress", "event": 0}',
                '{"op": "progress", "event": ""}',
                '{"op": "result", "result": [1]}',
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


class _LateWatchdog:
    """A watchdog that fires as the host cancels it: just after a full request line arrived."""

    def __init__(self, _: float, function: Any, args: tuple[Any, ...]) -> None:
        """Keep the kill to run at the cancel.

        Args:
            _: The silence limit; not used.
            function: The kill.
            args: The kill's arguments.
        """
        self.function = function
        self.args = args

    def start(self) -> None:
        """Start nothing; the kill runs at the cancel."""

    def cancel(self) -> None:
        """Run the kill, as a timer that fired before the cancel would."""
        self.function(*self.args)

    def join(self) -> None:
        """Return at once; the kill already ran."""


def test_a_request_that_arrives_as_the_watchdog_fires_is_not_served(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """When the watchdog kills the child just after its request arrives, the host does not call the model.

    Args:
        tmp_path: Pytest's temporary folder.
        monkeypatch: Makes the watchdog fire late and records the child.
    """
    monkeypatch.setattr(autosaddler, "threading", SimpleNamespace(**{**vars(threading), "Timer": _LateWatchdog}))
    children = _record_children(monkeypatch)
    calls: list[Any] = []
    request = json.dumps({"op": "model", "prompt": [{"role": "user", "content": "go"}], "tools": None})
    script = f"import sys\nsys.stdin.readline()\nprint({request!r}, flush=True)\nsys.stdin.readline()\n"
    ctx = make_ctx(
        str(tmp_path),
        call_model=lambda *args: calls.append(args) or ModelReply("x", Decimal(1), []),
        run_command=lambda *_: None,
        put_files=lambda *_: None,
        read_file=lambda *_: None,
        list_files=lambda *_: None,
    )

    with pytest.raises(ServiceError, match="sent no request"):
        _runner(tmp_path, script).run(Task("seed"), EvalServer(lambda *_: (1.0, {}), max_evals=1), ctx)

    assert not calls
    assert children[0].poll() is not None


def test_a_failed_model_call_sends_its_committed_cost(tmp_path: Path) -> None:
    """A model failure that the provider billed reaches the child with its cost.

    Args:
        tmp_path: Pytest's temporary folder.
    """

    def fail(*_: Any) -> ModelReply:
        """Fail as a billed model call does.

        Raises:
            ModelCallFailedError: Always, with a cost of 3 cents.
        """
        raise ModelCallFailedError("ConnectionError", cost_cents=Decimal(3))

    ctx = make_ctx(str(tmp_path), call_model=fail)

    reply = autosaddler._model(ctx, {"prompt": [{"role": "user", "content": "go"}]})

    assert reply == {"error": "ModelCallFailedError: The model call failed (ConnectionError).", "cost_cents": "3"}


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


def test_a_progress_event_with_a_boolean_field_is_dropped(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    """A progress event with ``true`` as its candidate id, case total or score reaches no run view.

    JSON ``true`` is a Python ``bool``, which is also an ``int``, so each field needs its own check.

    Args:
        tmp_path: Pytest's temporary folder.
        monkeypatch: Records the version and case events.
    """
    shown: list[dict[str, Any]] = []
    cases: list[dict[str, Any]] = []
    monkeypatch.setattr(autosaddler, "emit_candidate", lambda _callback, **fields: shown.append(fields))
    monkeypatch.setattr(autosaddler, "emit_case_scored", lambda _callback, **fields: cases.append(fields))
    version = {"score": 1.0, "candidate": "good", "per_example": []}
    case = {"event": "case_scored", "example_id": "0", "score": 1.0, "total": 1}
    lines = [
        {"op": "progress", "event": {**version, "candidate_id": True}},
        {"op": "progress", "event": {**version, "candidate_id": 1, "score": True}},
        {"op": "progress", "event": {**case, "candidate_id": True}},
        {"op": "progress", "event": {**case, "candidate_id": 1, "total": True}},
        {"op": "progress", "event": {**case, "candidate_id": 1, "score": True}},
    ]

    result = _runner(tmp_path, _chat_script(lines)).run(
        Task("seed"), EvalServer(lambda *_: (1.0, {}), max_evals=1), _bare_ctx(tmp_path)
    )

    assert result.metadata["replies"] == [{}] * len(lines)
    assert shown == []
    assert cases == []


def test_the_child_stop_reason_reaches_the_result_metadata(tmp_path: Path) -> None:
    """The stop reason in the child's result document reaches the engine result's metadata.

    Args:
        tmp_path: Pytest's temporary folder.
    """
    result_document = {"best_candidate": "seed", "best_score": 1.0, "stop_reason": "budget_reached"}
    script = f"import sys\nsys.stdin.readline()\nprint({json.dumps({'op': 'result', 'result': result_document})!r})\n"

    result = _runner(tmp_path, script).run(
        Task("seed"), EvalServer(lambda *_: (1.0, {}), max_evals=1), _bare_ctx(tmp_path)
    )

    assert result.metadata["stop_reason"] == "budget_reached"


def test_scorer_feedback_names_the_case_index(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    """Scorer feedback for an evaluation on a case names that case by its index.

    Args:
        tmp_path: Pytest's temporary folder.
        monkeypatch: Records the scorer feedback.
    """
    feedback: list[dict[str, Any]] = []
    monkeypatch.setattr(autosaddler, "emit_scorer_feedback", lambda _callback, **fields: feedback.append(fields))
    lines = [{"op": "evaluate", "candidate": "seed", "example": {"id": 0}, "candidate_id": 4}]

    _runner(tmp_path, _chat_script(lines)).run(
        Task("seed", cases=["a"]), EvalServer(lambda *_: (1.0, {}), max_evals=1), _bare_ctx(tmp_path)
    )

    assert [(fields["example_id"], fields["candidate_id"]) for fields in feedback] == [("0", "4")]


def test_a_refused_put_files_is_an_error_reply_and_the_run_goes_on(tmp_path: Path) -> None:
    """A tree store that refuses the child's files with a ValueError sends an error reply, not a run failure.

    Args:
        tmp_path: Pytest's temporary folder.
    """

    def refuse(*_: Any) -> str:
        """Refuse the files as the tree store does for a bad path.

        Raises:
            ValueError: Always.
        """
        raise ValueError("bad path")

    ctx = replace(_bare_ctx(tmp_path), put_files=refuse)
    lines = [{"op": "put_files", "files": {"a.txt": "eA=="}, "base_tree_id": None}]

    result = _runner(tmp_path, _chat_script(lines)).run(
        Task("seed"), EvalServer(lambda *_: (1.0, {}), max_evals=1), ctx
    )

    assert result.best_candidate == "seed"
    assert result.metadata["replies"] == [{"error": "ValueError: bad path"}]


def test_list_files_with_no_path_lists_the_tree_root(tmp_path: Path) -> None:
    """A list request with no path asks the loop for the whole tree, with an empty path.

    Args:
        tmp_path: Pytest's temporary folder.
    """
    asked: list[tuple[str, str]] = []

    def list_files(tree_id: str, path: str) -> list[str]:
        """Record the request and list one file.

        Args:
            tree_id: The tree.
            path: The folder in the tree.

        Returns:
            One path.
        """
        asked.append((tree_id, path))
        return ["a.txt"]

    ctx = replace(_bare_ctx(tmp_path), list_files=list_files)
    lines = [{"op": "list_files", "tree_id": "t"}]

    result = _runner(tmp_path, _chat_script(lines)).run(
        Task("seed"), EvalServer(lambda *_: (1.0, {}), max_evals=1), ctx
    )

    assert asked == [("t", "")]
    assert result.metadata["replies"] == [{"paths": ["a.txt"]}]
