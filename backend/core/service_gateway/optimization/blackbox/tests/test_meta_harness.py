"""Tests for Meta-Harness in the worker engine loop (SKY-722).

The tests use the real engine loop over a SQLite file, local subprocess boxes,
a scripted fake model and a pure Python scorer: no network and no paid call.
"""

from __future__ import annotations

import dataclasses
import json
import re
from collections.abc import Mapping
from decimal import Decimal
from pathlib import Path
from types import SimpleNamespace
from typing import Any
from unittest.mock import MagicMock

import pytest
from gepa.oa.config import OptimizeAnythingConfig
from sqlalchemy.engine import Engine

from core.constants import PROGRESS_CANDIDATE, PROGRESS_CASE_SCORED
from core.exceptions import ServiceError
from core.sandbox.trees import BoxFailureError, TreeRejectedError
from core.service_gateway.optimization.blackbox import meta_harness as meta_mod
from core.service_gateway.optimization.blackbox.harness_bridge import hidden_hashes
from core.service_gateway.optimization.blackbox.meta_harness import BASH_TOOL, MetaHarnessEngine
from core.service_gateway.optimization.blackbox.protocol import (
    EngineContext,
    EvalServer,
    ModelReply,
    RepoSeed,
    Result,
    Task,
)
from core.storage.step_log_store import StepLogStore
from core.storage.tree_store import TreeStore
from core.worker.actors import User
from core.worker.engine_loop import EngineLoop
from core.worker.tests import test_engine_loop as loop_tests
from core.worker.tests.test_engine_loop import (
    RUN_ID,
    _Boxes,
    _CountingSource,
    _new_generation,
    _steps,
    _WorkerDiedError,
)

# The engine-loop fixtures: a SQLite file, its step log and tree store, and alice with a run budget.
engine = loop_tests.engine
step_log = loop_tests.step_log
trees = loop_tests.trees
owner = loop_tests.owner

_CASES = [{"id": "a"}, {"id": "b"}]


class _Proposer:
    """A scripted coding agent: one command that writes a longer candidate, then a final reply."""

    def __init__(self, *, die_at: int | None = None, cost: str = "1", call: dict[str, Any] | None = None) -> None:
        """Set the script.

        Args:
            die_at: The index of the call in which the worker dies; ``None`` never dies.
            cost: The cost of each reply, in cents.
            call: A tool call to send in place of the scripted command.
        """
        self.die_at = die_at
        self.cost = Decimal(cost)
        self.call = call
        self.calls: list[list[dict[str, Any]]] = []
        self.tools: list[list[dict[str, Any]] | None] = []

    def __call__(self, prompt: str | list[dict[str, Any]], tools: list[dict[str, Any]] | None) -> ModelReply:
        """Answer one turn of the session.

        Args:
            prompt: The session's messages.
            tools: The tool definitions.

        Returns:
            A bash call on the first turn, and a reply with no tool call after a tool result.

        Raises:
            _WorkerDiedError: In the call at ``die_at``.
        """
        if len(self.calls) == self.die_at:
            raise _WorkerDiedError
        assert isinstance(prompt, list)
        self.calls.append([dict(message) for message in prompt])
        self.tools.append(tools)
        if prompt[-1]["role"] == "tool":
            return ModelReply("done", self.cost, [])
        if self.call is not None:
            return ModelReply("", self.cost, [self.call])
        user = next(message["content"] for message in prompt if message["role"] == "user")
        iteration = int(re.search(r"Run iteration (\d+)", user).group(1))
        name = f"c{iteration}"
        pending = json.dumps({"candidates": [{"name": name, "file": f"agents/{name}.txt"}]})
        command = f"printf %s {'seed' + 'x' * iteration} > agents/{name}.txt && printf '%s' '{pending}' > logs/run/pending_eval.json"
        return ModelReply(
            "", self.cost, [{"id": f"call-{iteration}", "name": "bash", "arguments": {"command": command}}]
        )


def _score(loop: EngineLoop, candidate: Any, case: Any) -> tuple[float | None, dict[str, Any]]:
    """Score a candidate by its length, with no box.

    Args:
        loop: The engine loop.
        candidate: The candidate text.
        case: Not used.

    Returns:
        The length and no side information.
    """
    del loop, case
    return float(len(str(candidate))), {}


def _loop(
    owner: tuple[User, int],
    engine: Engine,
    step_log: StepLogStore,
    trees: TreeStore,
    tmp_path: Path,
    model: _Proposer,
    *,
    generation: int,
    hidden: Mapping[str, str] | None = None,
) -> tuple[EngineLoop, _Boxes]:
    """Start the run at a generation and build a loop over local boxes and the scripted model.

    Args:
        owner: Alice and her budget generation.
        engine: The database.
        step_log: The step log.
        trees: The tree store.
        tmp_path: Pytest's temporary folder.
        model: The scripted model.
        generation: The job's execution generation.
        hidden: The run's hidden-path hashes.

    Returns:
        The loop and its box counter.
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
    loop = EngineLoop(
        run,
        engine=engine,
        trees=trees,
        candidates=_CountingSource(boxes),
        metrics=_CountingSource(boxes),
        model=model,
        model_max_cents=2,
        run_dir=tmp_path / f"loop-{generation}",
        hidden_hashes=hidden,
    )
    return loop, boxes


def _run(loop: EngineLoop, *, events: list[tuple[str, dict[str, Any]]] | None = None, **fields: Any) -> Result:
    """Run Meta-Harness for two iterations on the loop.

    Args:
        loop: The engine loop.
        events: A list that collects the progress events.
        **fields: Other context fields.

    Returns:
        The engine's result.
    """
    sink = None if events is None else (lambda kind, data: events.append((kind, data)))
    ctx = loop.context(max_iterations=2, progress_callback=sink, **fields)
    return MetaHarnessEngine().run(Task("seed", cases=list(_CASES)), loop.server(_score, max_evals=20), ctx)


def test_the_loop_path_never_starts_the_native_runtime(
    owner: tuple[User, int],
    engine: Engine,
    step_log: StepLogStore,
    trees: TreeStore,
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """With the worker host, the engine runs the native loop in the worker, not through the box runtime."""
    native = MagicMock(side_effect=AssertionError("the native runtime ran"))
    monkeypatch.setattr(meta_mod, "run_native_engine", native)
    loop, _ = _loop(owner, engine, step_log, trees, tmp_path, _Proposer(), generation=1)

    assert _run(loop).best_candidate == "seedxx"
    native.assert_not_called()


def test_a_context_without_the_worker_host_runs_the_native_runtime(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Until the SKY-728 cutover, a run outside the worker engine loop keeps the native box runtime."""
    incumbent = Result(best_candidate="native", best_score=0.5, total_evals=3)
    native = MagicMock(return_value=incumbent)
    monkeypatch.setattr(meta_mod, "run_native_engine", native)
    task = Task("seed")
    server = EvalServer(lambda candidate, example: (1.0, {}), max_evals=5)
    ctx = EngineContext(reflection_lm=str, run_dir=str(tmp_path))

    assert MetaHarnessEngine().run(task, server, ctx) is incumbent
    native.assert_called_once_with("meta_harness", task, server, ctx)


def test_each_model_reply_and_each_command_commits_one_step(
    owner: tuple[User, int], engine: Engine, step_log: StepLogStore, trees: TreeStore, tmp_path: Path
) -> None:
    """Each iteration is a model reply, its command and the final reply; the climb finds the best candidate."""
    model = _Proposer()
    loop, boxes = _loop(owner, engine, step_log, trees, tmp_path, model, generation=1)

    result = _run(loop)

    assert (result.best_candidate, result.best_score) == ("seedxx", 6.0)
    assert result.total_evals == 6
    assert [kind for _, kind, _ in _steps(engine)] == ["model", "command", "model"] * 2
    assert boxes.opened == 2
    assert not +boxes.open
    assert model.tools == [[BASH_TOOL]] * 4
    # The command result goes back to the model as the tool result of its call.
    tool_message = model.calls[1][-1]
    assert tool_message["role"] == "tool"
    assert tool_message["tool_call_id"] == "call-1"
    assert json.loads(tool_message["content"])["exit_code"] == 0


def test_a_run_killed_mid_climb_resumes_from_its_last_step(
    owner: tuple[User, int], engine: Engine, step_log: StepLogStore, trees: TreeStore, tmp_path: Path
) -> None:
    """Kill the worker in the third model call, then resume: no committed reply or command runs again."""
    first, _ = _loop(owner, engine, step_log, trees, tmp_path, _Proposer(die_at=2), generation=1)
    with pytest.raises(_WorkerDiedError):
        _run(first)
    assert [kind for _, kind, _ in _steps(engine)] == ["model", "command", "model"]

    _new_generation(engine)
    model = _Proposer()
    loop, boxes = _loop(owner, engine, step_log, trees, tmp_path, model, generation=2)
    result = _run(loop)

    assert (result.best_candidate, result.best_score) == ("seedxx", 6.0)
    # The first session comes back from the log; only the second session's calls run.
    assert len(model.calls) == 2
    assert boxes.opened == 1
    steps = _steps(engine)
    assert [kind for _, kind, _ in steps] == ["model", "command", "model"] * 2
    assert [generation for _, _, generation in steps] == [1, 1, 1, 2, 2, 2]


def test_the_climb_view_gets_its_progress_events(
    owner: tuple[User, int], engine: Engine, step_log: StepLogStore, trees: TreeStore, tmp_path: Path
) -> None:
    """Each benchmarked version sends its scored cases and one candidate event, as the native runtime does."""
    loop, _ = _loop(owner, engine, step_log, trees, tmp_path, _Proposer(), generation=1)
    events: list[tuple[str, dict[str, Any]]] = []

    _run(loop, events=events)

    cases = [
        (data["trial"], data["example_id"], data["score"]) for kind, data in events if kind == PROGRESS_CASE_SCORED
    ]
    assert cases == [(0, "0", 4.0), (0, "1", 4.0), (1, "0", 5.0), (1, "1", 5.0), (2, "0", 6.0), (2, "1", 6.0)]
    candidates = [data for kind, data in events if kind == PROGRESS_CANDIDATE]
    assert [(data["candidate_id"], data["score"], data["per_example"]) for data in candidates] == [
        ("0", 4.0, [{"id": "0", "score": 4.0}, {"id": "1", "score": 4.0}]),
        ("1", 5.0, [{"id": "0", "score": 5.0}, {"id": "1", "score": 5.0}]),
        ("2", 6.0, [{"id": "0", "score": 6.0}, {"id": "1", "score": 6.0}]),
    ]


def test_a_bad_tool_call_gets_an_error_result_and_no_step(
    owner: tuple[User, int], engine: Engine, step_log: StepLogStore, trees: TreeStore, tmp_path: Path
) -> None:
    """A call to an unknown tool goes back to the model as an error, and opens no box."""
    model = _Proposer(call={"id": "x", "name": "edit", "arguments": "{}"})
    loop, boxes = _loop(owner, engine, step_log, trees, tmp_path, model, generation=1)

    result = _run(loop)

    assert result.best_candidate == "seed"
    assert boxes.opened == 0
    assert [kind for _, kind, _ in _steps(engine)] == ["model", "model"] * 2
    assert "error" in json.loads(model.calls[1][-1]["content"])


def test_the_proposer_cost_cap_stops_the_session(
    owner: tuple[User, int], engine: Engine, step_log: StepLogStore, trees: TreeStore, tmp_path: Path
) -> None:
    """With a cap of one cent, the session runs its first reply's command and then asks the model no more."""
    model = _Proposer(cost="1")
    loop, boxes = _loop(owner, engine, step_log, trees, tmp_path, model, generation=1)
    options = SimpleNamespace(model="m", max_token_cost=0.01, proposer=SimpleNamespace(max_candidates_per_iter=None))

    result = _run(loop, native_options=options)

    assert result.best_candidate == "seedx"
    assert boxes.opened == 1
    assert [kind for _, kind, _ in _steps(engine)] == ["model", "command"]
    assert result.metadata["stop_reason"] == "proposer_budget"


def test_a_repository_run_is_refused(
    owner: tuple[User, int], engine: Engine, step_log: StepLogStore, trees: TreeStore, tmp_path: Path
) -> None:
    """A repository run needs ``save.sh`` and git in the box, which this port does not give yet."""
    model = _Proposer()
    loop, _ = _loop(owner, engine, step_log, trees, tmp_path, model, generation=1)
    seed = RepoSeed(tree_id="tree")

    with pytest.raises(ServiceError, match="repository"):
        _run(loop, repo_seed=seed)
    assert model.calls == []


def test_a_bash_call_cannot_read_a_hidden_path(
    owner: tuple[User, int], engine: Engine, step_log: StepLogStore, trees: TreeStore, tmp_path: Path
) -> None:
    """The bash tool runs through the loop, so ``cat`` of the hidden ``eval/`` and ``optimize.toml`` gives nothing."""
    hidden = hidden_hashes(["eval", "optimize.toml"])
    loop, _ = _loop(owner, engine, step_log, trees, tmp_path, _Proposer(), generation=1, hidden=hidden)
    tree = loop.put_files({"eval/metric.py": b"secret", "optimize.toml": b"secret", "src/a.py": b"a"})
    ctx = loop.context(repo_seed=RepoSeed(tree, hidden_hashes=hidden))
    config = OptimizeAnythingConfig(engine="meta_harness", run_dir=str(tmp_path / "mh"), engine_config={"model": ""})
    worker = meta_mod._WorkerMetaHarness(config, ctx)

    command = "cat eval/metric.py optimize.toml src/a.py; find . -type f | sort"
    output, after = worker._exec(tree, {"id": "c", "name": "bash", "arguments": {"command": command}})

    result = json.loads(output)
    assert "secret" not in result["stdout"]
    assert result["stdout"].split() == ["a./src/a.py"]
    assert loop.list_files(after) == ["src/a.py"]


def _worker(loop: EngineLoop, tmp_path: Path, ctx: EngineContext | None = None) -> Any:
    """Build the worker engine over the loop, with its work folders in place.

    Args:
        loop: The engine loop.
        tmp_path: Pytest's temporary folder.
        ctx: The context; the loop's own context when ``None``.

    Returns:
        The worker engine.
    """
    config = OptimizeAnythingConfig(engine="meta_harness", run_dir=str(tmp_path / "mh"), engine_config={"model": ""})
    worker = meta_mod._WorkerMetaHarness(config, ctx or loop.context())
    worker.logs_dir.mkdir(parents=True)
    (worker.work_dir / "agents").mkdir()
    return worker


def _pending(*files: str) -> bytes:
    """Return a ``pending_eval.json`` that lists the files.

    Args:
        *files: The candidate files.

    Returns:
        The JSON bytes.
    """
    return json.dumps({"candidates": [{"name": f"c{i}", "file": file} for i, file in enumerate(files)]}).encode()


@pytest.mark.parametrize(
    "error", [TreeRejectedError("r:a:1", "the tree is over its size limit"), BoxFailureError("r:a:1", "box died")]
)
def test_a_failed_command_goes_back_to_the_model_and_keeps_the_tree(
    error: Exception,
    owner: tuple[User, int],
    engine: Engine,
    step_log: StepLogStore,
    trees: TreeStore,
    tmp_path: Path,
) -> None:
    """A refused tree or a dead box is an error tool result, not the end of the run; the tree does not change."""
    loop, _ = _loop(owner, engine, step_log, trees, tmp_path, _Proposer(), generation=1)

    def fail(tree_id: str, command: str, grants: tuple[Any, ...]) -> Any:
        """Raise the box error, as the loop does live and on a replay.

        Args:
            tree_id: Not used.
            command: Not used.
            grants: Not used.

        Raises:
            Exception: The error.
        """
        raise error

    worker = _worker(loop, tmp_path, dataclasses.replace(loop.context(), run_command=fail))

    output, tree = worker._exec("tree-1", {"id": "c", "name": "bash", "arguments": {"command": "true"}})

    assert tree == "tree-1"
    assert str(error) in json.loads(output)["error"]


def test_a_pending_entry_that_clashes_with_a_local_folder_or_file_is_skipped(
    owner: tuple[User, int], engine: Engine, step_log: StepLogStore, trees: TreeStore, tmp_path: Path
) -> None:
    """An entry that names a local folder, or a path under a local file, changes nothing and does not raise."""
    loop, _ = _loop(owner, engine, step_log, trees, tmp_path, _Proposer(), generation=1)
    worker = _worker(loop, tmp_path)
    (worker.work_dir / "agents" / "v").mkdir()
    (worker.work_dir / "agents" / "v" / "a.txt").write_text("a")
    (worker.work_dir / "agents" / "x.txt").write_text("x")
    tree = loop.put_files(
        {
            "logs/run/pending_eval.json": _pending("agents/v", "agents/x.txt/y"),
            "agents/v/a.txt": b"a",
            "agents/x.txt/y": b"y",
        }
    )

    worker._pull(tree)

    assert (worker.work_dir / "agents" / "v" / "a.txt").read_text() == "a"
    assert (worker.work_dir / "agents" / "x.txt").read_text() == "x"


def test_a_pending_entry_outside_agents_is_not_copied_or_deleted(
    owner: tuple[User, int], engine: Engine, step_log: StepLogStore, trees: TreeStore, tmp_path: Path
) -> None:
    """Only files under ``agents/`` come back from the tree; a path outside it is neither written nor deleted."""
    loop, _ = _loop(owner, engine, step_log, trees, tmp_path, _Proposer(), generation=1)
    worker = _worker(loop, tmp_path)
    outside = worker.work_dir.parent / "outside.txt"
    outside.write_text("keep")
    (worker.work_dir / "task.md").write_text("keep")
    tree = loop.put_files({"logs/run/pending_eval.json": _pending("../outside.txt", "task.md"), "task.md": b"changed"})

    worker._pull(tree)

    assert outside.read_text() == "keep"
    assert (worker.work_dir / "task.md").read_text() == "keep"


def test_the_worker_path_keeps_clock_values_out_of_the_workspace(
    owner: tuple[User, int], engine: Engine, step_log: StepLogStore, trees: TreeStore, tmp_path: Path
) -> None:
    """The summary rows that go into the tree hold no timing, so a resume puts the same files in the tree."""
    loop, _ = _loop(owner, engine, step_log, trees, tmp_path, _Proposer(), generation=1)

    _run(loop)

    summary = tmp_path / "loop-1" / "meta_harness" / "logs" / "run" / "evolution_summary.jsonl"
    rows = [json.loads(line) for line in summary.read_text().splitlines()]
    assert [row["system"] for row in rows] == ["c1", "c2"]
    assert all("timing_s" not in row for row in rows)


def test_the_worker_skill_and_tool_keep_scratch_files_in_the_workspace(tmp_path: Path) -> None:
    """``/tmp`` does not persist between commands, so the skill and the tool both point to ``scratch/``."""
    config = OptimizeAnythingConfig(engine="meta_harness", run_dir=str(tmp_path), engine_config={"model": ""})
    skill = meta_mod._WorkerMetaHarness(config, EngineContext(reflection_lm=str, run_dir=str(tmp_path))).skill()

    assert "/tmp/" not in skill
    assert "scratch file under `scratch/` in the workspace" in skill
    assert "`scratch/`" in BASH_TOOL["description"]
