"""Tests for Meta-Harness in the worker engine loop (SKY-722).

The tests use the real engine loop over a SQLite file, local subprocess boxes,
a scripted fake model and a pure Python scorer: no network and no paid call.
"""

from __future__ import annotations

import dataclasses
import json
import logging
import math
import re
from collections.abc import Mapping
from decimal import Decimal
from pathlib import Path
from types import SimpleNamespace
from typing import Any
from unittest.mock import MagicMock

import pytest
from gepa.oa.budget import BudgetExhausted
from gepa.oa.config import OptimizeAnythingConfig
from sqlalchemy.engine import Engine

from core.billing.model_gateway import MCP_RELAY_ROUTE_KEY
from core.billing.usage_tags import current_tags
from core.constants import PROGRESS_CANDIDATE, PROGRESS_CASE_SCORED, PROGRESS_MINIBATCH
from core.exceptions import ServiceError
from core.models.blackbox import BLACKBOX_HARNESS_CUSTOM, BLACKBOX_HARNESS_PI, BlackboxProposer
from core.sandbox.trees import BoxFailureError, TreeRejectedError
from core.service_gateway.optimization.blackbox import loop_tools, native_engines
from core.service_gateway.optimization.blackbox import meta_harness as meta_mod
from core.service_gateway.optimization.blackbox.harness_bridge import hidden_hashes
from core.service_gateway.optimization.blackbox.loop_tools import OUTPUT_BYTES
from core.service_gateway.optimization.blackbox.meta_harness import BASH_TOOL, MetaHarnessEngine
from core.service_gateway.optimization.blackbox.native_engines import ProposerFailedError
from core.service_gateway.optimization.blackbox.protocol import (
    BudgetExhaustedError,
    EngineContext,
    EvalServer,
    ModelReply,
    RepoSeed,
    Result,
    Task,
)
from core.storage.object_store import LocalObjectStore
from core.storage.step_log_store import StepLogStore
from core.storage.tree_store import TreeStore
from core.worker.actors import User
from core.worker.engine_loop import EngineLoop, ModelCallBilledError, ModelCallFailedError
from core.worker.tests import test_engine_loop as loop_tests
from core.worker.tests.test_engine_loop import (
    RUN_ID,
    _Boxes,
    _CountingSource,
    _new_generation,
    _steps,
    _store,
    _tree,
    _WorkerDiedError,
)

# The engine-loop fixtures: a SQLite file, its step log and tree store, and alice with a run budget.
engine = loop_tests.engine
step_log = loop_tests.step_log
trees = loop_tests.trees
owner = loop_tests.owner

_CASES = [{"id": "a"}, {"id": "b"}]


@pytest.fixture(autouse=True)
def sleeps(monkeypatch: pytest.MonkeyPatch) -> list[float]:
    """Record the retry waits in place of sleeping, so no test waits.

    Args:
        monkeypatch: Pytest's monkeypatch.

    Returns:
        The waits, in seconds, in order.
    """
    waits: list[float] = []
    monkeypatch.setattr(loop_tools, "_sleep", waits.append)
    return waits


class _Proposer:
    """A scripted coding agent: one command that writes a longer candidate, then a final reply."""

    def __init__(
        self,
        *,
        die_at: int | None = None,
        cost: str = "1",
        call: dict[str, Any] | None = None,
        fail_at: frozenset[int] = frozenset(),
        billed_at: frozenset[int] = frozenset(),
        output_chars: int = 0,
        output_letter: str = "y",
    ) -> None:
        """Set the script.

        Args:
            die_at: The index of the call in which the worker dies; ``None`` never dies.
            cost: The cost of each reply, in cents.
            call: A tool call to send in place of the scripted command.
            fail_at: The indexes of the attempts, failed ones included, that fail as a rate limit does.
            billed_at: The indexes of the attempts that fail after the provider billed one cent.
            output_chars: The length of the extra stdout and stderr that the scripted command prints.
            output_letter: The letter of that output, as Python source text.
        """
        self.die_at = die_at
        self.fail_at = fail_at
        self.billed_at = billed_at
        self.attempts = 0
        self.cost = Decimal(cost)
        self.call = call
        self.output_chars = output_chars
        self.output_letter = output_letter
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
            RuntimeError: In the attempts of ``fail_at``.
            ModelCallBilledError: In the attempts of ``billed_at``.
        """
        if len(self.calls) == self.die_at:
            raise _WorkerDiedError
        self.attempts += 1
        if self.attempts - 1 in self.fail_at:
            raise RuntimeError("429 rate limited")
        if self.attempts - 1 in self.billed_at:
            raise ModelCallBilledError("1")
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
        command = (
            f"printf %s {'seed' + 'x' * iteration} > agents/{name}.txt"
            f" && printf '%s' '{pending}' > logs/run/pending_eval.json"
        )
        if self.output_chars:
            # Bytes, so a Hebrew letter does not depend on the box's locale.
            text = f"('{self.output_letter}' * {self.output_chars}).encode()"
            command += f' && python3 -c "import sys; sys.stdout.buffer.write({text}); sys.stderr.buffer.write({text})"'
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


def _run(
    loop: EngineLoop,
    *,
    events: list[tuple[str, dict[str, Any]]] | None = None,
    scorer: Any = _score,
    max_evals: int = 20,
    **fields: Any,
) -> Result:
    """Run Meta-Harness for two iterations on the loop.

    Args:
        loop: The engine loop.
        events: A list that collects the progress events.
        scorer: The step scorer.
        max_evals: The scorer-run budget.
        **fields: Other context fields.

    Returns:
        The engine's result.
    """
    sink = None if events is None else (lambda kind, data: events.append((kind, data)))
    ctx = loop.context(max_iterations=2, progress_callback=sink, **fields)
    return MetaHarnessEngine().run(Task("seed", cases=list(_CASES)), loop.server(scorer, max_evals=max_evals), ctx)


def _options(
    *,
    max_token_cost: float | None = None,
    reasoning_effort: str | None = None,
    budget_route: Any = None,
    **proposer: Any,
) -> SimpleNamespace:
    """Build the native options that the worker path reads.

    Args:
        max_token_cost: The proposer cost cap, in dollars.
        reasoning_effort: The model's effort level.
        budget_route: The run's budget route.
        **proposer: The ``[proposer]`` settings.

    Returns:
        The options.
    """
    return SimpleNamespace(
        model="m",
        max_token_cost=max_token_cost,
        reasoning_effort=reasoning_effort,
        budget_route=budget_route,
        proposer=BlackboxProposer(**proposer),
    )


def test_the_loop_path_never_starts_the_native_runtime(
    owner: tuple[User, int],
    engine: Engine,
    step_log: StepLogStore,
    trees: TreeStore,
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """With the worker host, the engine runs the native loop in the worker, not through the box runtime.

    Args:
        owner: Alice and her budget generation.
        engine: The database.
        step_log: The step log.
        trees: The tree store.
        tmp_path: Pytest's temporary folder.
        monkeypatch: Pytest's monkeypatch.
    """
    native = MagicMock(side_effect=AssertionError("the native runtime ran"))
    monkeypatch.setattr(meta_mod, "run_native_engine", native)
    loop, _ = _loop(owner, engine, step_log, trees, tmp_path, _Proposer(), generation=1)

    assert _run(loop).best_candidate == "seedxx"
    native.assert_not_called()


def test_a_context_without_the_worker_host_runs_the_native_runtime(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Until the SKY-728 cutover, a run outside the worker engine loop keeps the native box runtime.

    Args:
        tmp_path: Pytest's temporary folder.
        monkeypatch: Pytest's monkeypatch.
    """
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
    """Each iteration is a model reply, its command and the final reply; the climb finds the best candidate.

    Args:
        owner: Alice and her budget generation.
        engine: The database.
        step_log: The step log.
        trees: The tree store.
        tmp_path: Pytest's temporary folder.
    """
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
    """Kill the worker in the third model call, then resume: no committed reply or command runs again.

    Args:
        owner: Alice and her budget generation.
        engine: The database.
        step_log: The step log.
        trees: The tree store.
        tmp_path: Pytest's temporary folder.
    """
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
    owner: tuple[User, int],
    engine: Engine,
    step_log: StepLogStore,
    trees: TreeStore,
    tmp_path: Path,
    caplog: pytest.LogCaptureFixture,
) -> None:
    """Each benchmarked version sends its scored cases, its notes and one candidate event, as the native runtime does.

    Each scorer call runs in the candidate's and case's scope, and the run log gets the native typed events.

    Args:
        owner: Alice and her budget generation.
        engine: The database.
        step_log: The step log.
        trees: The tree store.
        tmp_path: Pytest's temporary folder.
        caplog: Pytest's log capture.
    """
    loop, _ = _loop(owner, engine, step_log, trees, tmp_path, _Proposer(), generation=1)
    events: list[tuple[str, dict[str, Any]]] = []
    scopes: list[tuple[str | None, str | None]] = []

    def score(loop: EngineLoop, candidate: Any, case: Any) -> tuple[float | None, dict[str, Any]]:
        """Score by length, keep the scope tags of the call, and leave a note.

        Args:
            loop: The engine loop.
            candidate: The candidate text.
            case: Not used.

        Returns:
            The length and a feedback note.
        """
        del loop, case
        tags = current_tags()
        scopes.append((tags.get("candidate"), tags.get("case")))
        return float(len(str(candidate))), {"feedback": f"length {len(str(candidate))}"}

    with caplog.at_level(logging.INFO, logger=meta_mod.logger.name):
        _run(loop, events=events, scorer=score)

    assert scopes == [("0", "0"), ("0", "1"), ("1", "0"), ("1", "1"), ("2", "0"), ("2", "1")]
    notes = [
        (data["candidate_id"], data["example_id"], data["feedback"])
        for kind, data in events
        if kind == PROGRESS_MINIBATCH
    ]
    assert notes[:2] == [("0", "0", "length 4"), ("0", "1", "length 4")]
    assert len(notes) == 6
    logged = [
        (record.run_log["event"], record.run_log["candidate"], record.run_log["case"], record.run_log["fields"])
        for record in caplog.records
        if record.name == meta_mod.logger.name
    ]
    assert logged[:3] == [
        ("case.scored", "0", "0", {"score": 4.0, "total": 2}),
        ("case.scored", "0", "1", {"score": 4.0, "total": 2}),
        ("candidate.scored", "0", None, {"score": 4.0, "total_evals": 2}),
    ]
    assert [event for event, *_ in logged].count("candidate.scored") == 3

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
    """A call to an unknown tool goes back to the model as an error, and opens no box.

    Args:
        owner: Alice and her budget generation.
        engine: The database.
        step_log: The step log.
        trees: The tree store.
        tmp_path: Pytest's temporary folder.
    """
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
    """With a cap of one cent, the session runs its first reply's command and then asks the model no more.

    Args:
        owner: Alice and her budget generation.
        engine: The database.
        step_log: The step log.
        trees: The tree store.
        tmp_path: Pytest's temporary folder.
    """
    model = _Proposer(cost="1")
    loop, boxes = _loop(owner, engine, step_log, trees, tmp_path, model, generation=1)
    result = _run(loop, native_options=_options(max_token_cost=0.01))

    assert result.best_candidate == "seedx"
    assert boxes.opened == 1
    assert [kind for _, kind, _ in _steps(engine)] == ["model", "command"]
    assert result.metadata["stop_reason"] == "proposer_budget"


def test_a_repository_run_is_refused(
    owner: tuple[User, int], engine: Engine, step_log: StepLogStore, trees: TreeStore, tmp_path: Path
) -> None:
    """A repository run needs ``save.sh`` and git in the box, which this port does not give yet.

    Args:
        owner: Alice and her budget generation.
        engine: The database.
        step_log: The step log.
        trees: The tree store.
        tmp_path: Pytest's temporary folder.
    """
    model = _Proposer()
    loop, _ = _loop(owner, engine, step_log, trees, tmp_path, model, generation=1)
    seed = RepoSeed(tree_id=_tree(trees, "a.py", "a"))

    with pytest.raises(ServiceError, match="repository"):
        _run(loop, repo_seed=seed)
    assert model.calls == []


@pytest.mark.parametrize(
    ("options", "field"),
    [
        (_options(run_command="my-agent"), "run_command"),
        (_options(install_command="pip install my-agent"), "install_command"),
        (_options(harness=BLACKBOX_HARNESS_PI), "harness"),
        (_options(harness=BLACKBOX_HARNESS_CUSTOM, run_command="my-agent"), "harness"),
        (_options(host_cli="codex"), "host_cli"),
        (_options(budget_route={MCP_RELAY_ROUTE_KEY: {"servers": [{"name": "docs"}]}}), "mcp"),
        (_options(reasoning_effort="high"), "reasoning_effort"),
    ],
)
def test_a_proposer_setting_that_the_tool_loop_drops_is_refused(
    options: SimpleNamespace,
    field: str,
    owner: tuple[User, int],
    engine: Engine,
    step_log: StepLogStore,
    trees: TreeStore,
    tmp_path: Path,
) -> None:
    """Until SKY-816, the tool loop is the only proposer: a setting that it would drop fails the run before any call.

    Args:
        options: The native options with the dropped setting.
        field: The setting that the error names.
        owner: Alice and her budget generation.
        engine: The database.
        step_log: The step log.
        trees: The tree store.
        tmp_path: Pytest's temporary folder.
    """
    model = _Proposer()
    loop, boxes = _loop(owner, engine, step_log, trees, tmp_path, model, generation=1)

    with pytest.raises(ServiceError, match=rf"cannot apply these settings: .*\b{field}\b"):
        _run(loop, native_options=options)
    assert model.calls == []
    assert boxes.opened == 0


def test_the_default_proposer_settings_are_not_refused(
    owner: tuple[User, int], engine: Engine, step_log: StepLogStore, trees: TreeStore, tmp_path: Path
) -> None:
    """The default ``[proposer]`` table, and a budget route with no MCP servers, run the tool loop.

    Args:
        owner: Alice and her budget generation.
        engine: The database.
        step_log: The step log.
        trees: The tree store.
        tmp_path: Pytest's temporary folder.
    """
    loop, _ = _loop(owner, engine, step_log, trees, tmp_path, _Proposer(), generation=1)

    result = _run(loop, native_options=_options(budget_route={MCP_RELAY_ROUTE_KEY: {"servers": []}}))

    assert result.best_candidate == "seedxx"


def test_a_task_that_holds_the_metric_source_is_refused(
    owner: tuple[User, int], engine: Engine, step_log: StepLogStore, trees: TreeStore, tmp_path: Path
) -> None:
    """As in the native runtime, a task document that holds the metric source never reaches the workspace.

    Args:
        owner: Alice and her budget generation.
        engine: The database.
        step_log: The step log.
        trees: The tree store.
        tmp_path: Pytest's temporary folder.
    """
    model = _Proposer()
    loop, boxes = _loop(owner, engine, step_log, trees, tmp_path, model, generation=1)
    metric = "def score(candidate):\n    return len(candidate)"
    task = Task("seed", objective=f"Maximize this:\n{metric}", cases=list(_CASES), context={"metric_code": metric})

    with pytest.raises(ServiceError, match="metric source"):
        MetaHarnessEngine().run(task, loop.server(_score, max_evals=20), loop.context(max_iterations=2))
    assert model.calls == []
    assert boxes.opened == 0


def test_a_bash_call_cannot_read_a_hidden_path(
    owner: tuple[User, int], engine: Engine, step_log: StepLogStore, trees: TreeStore, tmp_path: Path
) -> None:
    """The bash tool runs through the loop, so ``cat`` of the hidden ``eval/`` and ``optimize.toml`` gives nothing.

    Args:
        owner: Alice and her budget generation.
        engine: The database.
        step_log: The step log.
        trees: The tree store.
        tmp_path: Pytest's temporary folder.
    """
    hidden = hidden_hashes(["eval", "optimize.toml"])
    loop, _ = _loop(owner, engine, step_log, trees, tmp_path, _Proposer(), generation=1, hidden=hidden)
    # The loop refuses to write a hidden path, so the seed tree goes straight into the tree store.
    tree = _store(trees, {"eval/metric.py": b"secret", "optimize.toml": b"secret", "src/a.py": b"a"})
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
    """A refused tree or a dead box is an error tool result, not the end of the run; the tree does not change.

    Args:
        error: The box error that the command raises.
        owner: Alice and her budget generation.
        engine: The database.
        step_log: The step log.
        trees: The tree store.
        tmp_path: Pytest's temporary folder.
    """
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
    """An entry that names a local folder, or a path under a local file, changes nothing and does not raise.

    Args:
        owner: Alice and her budget generation.
        engine: The database.
        step_log: The step log.
        trees: The tree store.
        tmp_path: Pytest's temporary folder.
    """
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
    """Only files under ``agents/`` come back from the tree; a path outside it is neither written nor deleted.

    Args:
        owner: Alice and her budget generation.
        engine: The database.
        step_log: The step log.
        trees: The tree store.
        tmp_path: Pytest's temporary folder.
    """
    loop, _ = _loop(owner, engine, step_log, trees, tmp_path, _Proposer(), generation=1)
    worker = _worker(loop, tmp_path)
    outside = worker.work_dir.parent / "outside.txt"
    outside.write_text("keep")
    (worker.work_dir / "task.md").write_text("keep")
    pending = _pending("../outside.txt", "agents/../../outside.txt", "task.md")
    tree = loop.put_files({"logs/run/pending_eval.json": pending, "task.md": b"changed"})

    worker._pull(tree)

    assert outside.read_text() == "keep"
    assert (worker.work_dir / "task.md").read_text() == "keep"


def test_the_worker_path_keeps_clock_values_out_of_the_workspace(
    owner: tuple[User, int], engine: Engine, step_log: StepLogStore, trees: TreeStore, tmp_path: Path
) -> None:
    """The summary rows that go into the tree hold no timing, so a resume puts the same files in the tree.

    Args:
        owner: Alice and her budget generation.
        engine: The database.
        step_log: The step log.
        trees: The tree store.
        tmp_path: Pytest's temporary folder.
    """
    loop, _ = _loop(owner, engine, step_log, trees, tmp_path, _Proposer(), generation=1)

    _run(loop)

    summary = tmp_path / "loop-1" / "meta_harness" / "logs" / "run" / "evolution_summary.jsonl"
    rows = [json.loads(line) for line in summary.read_text().splitlines()]
    assert [row["system"] for row in rows] == ["c1", "c2"]
    assert all("timing_s" not in row for row in rows)


def test_the_worker_skill_and_tool_keep_scratch_files_in_the_workspace(tmp_path: Path) -> None:
    """``/tmp`` does not persist between commands, so the skill and the tool both point to ``scratch/``.

    Args:
        tmp_path: Pytest's temporary folder.
    """
    config = OptimizeAnythingConfig(engine="meta_harness", run_dir=str(tmp_path), engine_config={"model": ""})
    skill = meta_mod._WorkerMetaHarness(config, EngineContext(reflection_lm=str, run_dir=str(tmp_path))).skill()

    assert "/tmp/" not in skill
    assert "scratch file under `scratch/` in the workspace" in skill
    assert "`scratch/`" in BASH_TOOL["description"]


def test_a_scorer_traceback_reaches_the_proposer_as_one_line(
    owner: tuple[User, int], engine: Engine, step_log: StepLogStore, trees: TreeStore, tmp_path: Path
) -> None:
    """A scorer error can hold metric source in its traceback; the workspace keeps only its last line.

    Args:
        owner: Alice and her budget generation.
        engine: The database.
        step_log: The step log.
        trees: The tree store.
        tmp_path: Pytest's temporary folder.
    """
    traceback = (
        "Traceback (most recent call last):\n"
        '  File "metric.py", line 3, in score\n'
        "    secret_metric_line()\n"
        "ValueError: bad case"
    )

    def score(loop: EngineLoop, candidate: Any, case: Any) -> tuple[float | None, dict[str, Any]]:
        """Fail the first case of ``seedx`` with a traceback, and score the rest by length.

        Args:
            loop: The engine loop.
            candidate: The candidate text.
            case: The case.

        Returns:
            The score and the side information.
        """
        del loop
        if candidate == "seedx" and case == _CASES[0]:
            return None, {"error": traceback}
        return float(len(str(candidate))), {}

    loop, _ = _loop(owner, engine, step_log, trees, tmp_path, _Proposer(), generation=1)
    events: list[tuple[str, dict[str, Any]]] = []

    _run(loop, events=events, scorer=score)

    results = tmp_path / "loop-1" / "meta_harness" / "logs" / "run" / "results"
    assert json.loads((results / "c1" / "val.json").read_text())["infos"]["0"] == {"error": "ValueError: bad case"}
    assert all("secret_metric_line" not in path.read_text() for path in results.rglob("*.json"))
    # The server keeps the raw error, so the crashed version still shows no score.
    candidates = [data for kind, data in events if kind == PROGRESS_CANDIDATE]
    assert [(data["candidate_id"], data["score"]) for data in candidates] == [("0", 4.0), ("1", None), ("2", 6.0)]


def test_a_long_command_output_is_cut_to_its_head_and_tail(
    owner: tuple[User, int], engine: Engine, step_log: StepLogStore, trees: TreeStore, tmp_path: Path
) -> None:
    """Each output keeps a fixed size, so the later prompts and their hashes stay bounded and stable.

    Args:
        owner: Alice and her budget generation.
        engine: The database.
        step_log: The step log.
        trees: The tree store.
        tmp_path: Pytest's temporary folder.
    """
    loop, _ = _loop(owner, engine, step_log, trees, tmp_path, _Proposer(), generation=1)
    worker = _worker(loop, tmp_path)
    tree = loop.put_files({"a.txt": b"a"})
    command = "python3 -c \"import sys; sys.stdout.write('a' * 40000 + 'b' * 40000); sys.stderr.write('e')\""

    first = json.loads(worker._exec(tree, {"id": "c", "name": "bash", "arguments": {"command": command}})[0])
    second = json.loads(worker._exec(tree, {"id": "c", "name": "bash", "arguments": {"command": command}})[0])

    stdout = first["stdout"]
    assert stdout == second["stdout"]
    kept = (80_000 - 50_042) // 2
    assert stdout == "a" * kept + "\n[... 50042 characters cut ...]\n" + "b" * kept
    assert loop_tools.prompt_bytes(stdout) <= OUTPUT_BYTES
    assert first["stderr"] == "e"


@pytest.mark.parametrize("letter", ["\x01", "א", "\U0001f600", "\udc80"])
def test_an_output_is_cut_to_its_bytes_in_the_prompt(letter: str) -> None:
    """A control, Hebrew, non-BMP or lone surrogate character takes more than one prompt byte, and the cut counts it.

    Args:
        letter: The one character of the output.
    """
    text = "<" + letter * 100_000 + ">"

    clipped = loop_tools.clip(text)

    assert clipped == loop_tools.clip(text)
    assert OUTPUT_BYTES - 64 <= loop_tools.prompt_bytes(clipped) <= OUTPUT_BYTES
    assert clipped.startswith("<" + letter)
    assert clipped.endswith(letter + ">")


def test_a_huge_output_is_cut_without_encoding_all_of_it(monkeypatch: pytest.MonkeyPatch) -> None:
    """Measure only short pieces of an output far over the cap, so a 64 MiB output costs no gigabytes.

    Args:
        monkeypatch: Records the length of each text that the cut measures.
    """
    measured: list[int] = []
    real_prompt_bytes = loop_tools.prompt_bytes

    def prompt_bytes(text: str) -> int:
        """Record the text length, then measure the text.

        Args:
            text: The text.

        Returns:
            Its prompt bytes.
        """
        measured.append(len(text))
        return real_prompt_bytes(text)

    monkeypatch.setattr(loop_tools, "prompt_bytes", prompt_bytes)

    clipped = loop_tools.clip("\x01" * 1_000_000)

    assert max(measured) < 100
    assert real_prompt_bytes(clipped) <= OUTPUT_BYTES


def test_one_command_of_control_and_hebrew_output_leaves_room_for_the_next_model_call(
    owner: tuple[User, int], engine: Engine, step_log: StepLogStore, trees: TreeStore, tmp_path: Path
) -> None:
    """A control character takes 7 prompt bytes and a Hebrew letter 2, so the cut keeps one result under the cap.

    Args:
        owner: Alice and her budget generation.
        engine: The database.
        step_log: The step log.
        trees: The tree store.
        tmp_path: Pytest's temporary folder.
    """
    script = "import sys; sys.stdout.buffer.write(bytes(30000)); sys.stderr.buffer.write(('\\u05d0' * 60000).encode())"
    model = _Proposer(call={"id": "b", "name": "bash", "arguments": {"command": f'python3 -c "{script}"'}})
    loop, _ = _loop(owner, engine, step_log, trees, tmp_path, model, generation=1)

    _run(loop)

    assert len(model.calls) >= 2
    result = json.loads(model.calls[1][-1]["content"])
    assert result["stdout"].startswith("\x00")
    assert result["stderr"].startswith("א")
    before, after = (
        len(json.dumps(call, ensure_ascii=False).encode("utf-8", "surrogatepass")) for call in model.calls[:2]
    )
    # The reply and the result add the two cut streams and a little framing, so the prompt stays under the cap.
    assert after - before < 2 * OUTPUT_BYTES + 1_000
    assert after < loop_tools.MAX_PROMPT_BYTES


def test_a_long_hebrew_output_leaves_room_for_the_next_model_call(
    owner: tuple[User, int], engine: Engine, step_log: StepLogStore, trees: TreeStore, tmp_path: Path
) -> None:
    """The prompt cap counts a Hebrew letter of a tool result as its two UTF-8 bytes, not as a six-character escape.

    The two outputs are under ``MAX_PROMPT_BYTES`` as bytes and past it when escaped, so the session goes on.

    Args:
        owner: Alice and her budget generation.
        engine: The database.
        step_log: The step log.
        trees: The tree store.
        tmp_path: Pytest's temporary folder.
    """
    letters = 14_000
    text = f"('\\u05d0' * {letters}).encode()"
    script = f"import sys; sys.stdout.buffer.write({text}); sys.stderr.buffer.write({text})"
    model = _Proposer(call={"id": "h", "name": "bash", "arguments": {"command": f'python3 -c "{script}"'}})
    loop, _ = _loop(owner, engine, step_log, trees, tmp_path, model, generation=1)

    _run(loop)

    assert len(model.calls) >= 2
    result = model.calls[1][-1]
    assert result["role"] == "tool"
    assert json.loads(result["content"])["stdout"] == "א" * letters
    assert len(json.dumps(model.calls[1], ensure_ascii=False).encode()) < loop_tools.MAX_PROMPT_BYTES
    assert len(json.dumps(model.calls[1])) > loop_tools.MAX_PROMPT_BYTES


def test_a_failed_model_call_is_tried_again_and_replays_the_same_way(
    owner: tuple[User, int], engine: Engine, step_log: StepLogStore, trees: TreeStore, tmp_path: Path
) -> None:
    """A transient failure commits a failed step and a retry; a resume replays both with no new model call.

    Args:
        owner: Alice and her budget generation.
        engine: The database.
        step_log: The step log.
        trees: The tree store.
        tmp_path: Pytest's temporary folder.
    """
    first, _ = _loop(
        owner, engine, step_log, trees, tmp_path, _Proposer(fail_at=frozenset({0}), die_at=2), generation=1
    )
    with pytest.raises(_WorkerDiedError):
        _run(first)
    assert [kind for _, kind, _ in _steps(engine)] == ["model", "model", "command", "model"]

    _new_generation(engine)
    model = _Proposer()
    loop, _ = _loop(owner, engine, step_log, trees, tmp_path, model, generation=2)
    result = _run(loop)

    assert (result.best_candidate, result.best_score) == ("seedxx", 6.0)
    # The failed step and its retry come back from the log; only the second session's calls run.
    assert model.attempts == 2
    steps = _steps(engine)
    assert [kind for _, kind, _ in steps] == ["model", "model", "command", "model", "model", "command", "model"]
    assert [generation for _, _, generation in steps] == [1, 1, 1, 1, 2, 2, 2]


def test_a_model_call_that_fails_on_every_attempt_ends_the_session_as_an_error(
    owner: tuple[User, int], engine: Engine, step_log: StepLogStore, trees: TreeStore, tmp_path: Path
) -> None:
    """After the fixed number of attempts, the session fails and the run stops with the proposer error.

    Args:
        owner: Alice and her budget generation.
        engine: The database.
        step_log: The step log.
        trees: The tree store.
        tmp_path: Pytest's temporary folder.
    """
    model = _Proposer(fail_at=frozenset({0, 1, 2}))
    loop, boxes = _loop(owner, engine, step_log, trees, tmp_path, model, generation=1)

    with pytest.raises(ProposerFailedError, match=re.escape(str(ModelCallFailedError("RuntimeError")))):
        _run(loop)
    assert model.attempts == loop_tools.MODEL_ATTEMPTS
    assert [kind for _, kind, _ in _steps(engine)] == ["model"] * loop_tools.MODEL_ATTEMPTS
    assert boxes.opened == 0


def test_a_live_failure_waits_before_its_retry_and_a_replayed_one_does_not(
    owner: tuple[User, int],
    engine: Engine,
    step_log: StepLogStore,
    trees: TreeStore,
    tmp_path: Path,
    sleeps: list[float],
) -> None:
    """A live failed call waits before its retry; its replay made no call, so it waits for nothing.

    Args:
        owner: Alice and her budget generation.
        engine: The database.
        step_log: The step log.
        trees: The tree store.
        tmp_path: Pytest's temporary folder.
        sleeps: The recorded retry waits.
    """
    first, _ = _loop(
        owner, engine, step_log, trees, tmp_path, _Proposer(fail_at=frozenset({0}), die_at=2), generation=1
    )
    with pytest.raises(_WorkerDiedError):
        _run(first)
    assert sleeps == [loop_tools.MODEL_RETRY_WAIT_SECONDS]

    sleeps.clear()
    _new_generation(engine)
    loop, _ = _loop(owner, engine, step_log, trees, tmp_path, _Proposer(), generation=2)
    _run(loop)

    assert sleeps == []


def test_no_wait_follows_the_last_failed_attempt(
    owner: tuple[User, int],
    engine: Engine,
    step_log: StepLogStore,
    trees: TreeStore,
    tmp_path: Path,
    sleeps: list[float],
) -> None:
    """The session waits only between attempts, so three failures wait twice.

    Args:
        owner: Alice and her budget generation.
        engine: The database.
        step_log: The step log.
        trees: The tree store.
        tmp_path: Pytest's temporary folder.
        sleeps: The recorded retry waits.
    """
    loop, _ = _loop(owner, engine, step_log, trees, tmp_path, _Proposer(fail_at=frozenset({0, 1, 2})), generation=1)

    with pytest.raises(ProposerFailedError):
        _run(loop)
    assert sleeps == [loop_tools.MODEL_RETRY_WAIT_SECONDS] * (loop_tools.MODEL_ATTEMPTS - 1)


@pytest.mark.parametrize(
    ("cap", "value", "output_chars"), [("max_tool_calls", 1, 0), ("MAX_PROMPT_BYTES", 20_000, 25_000)]
)
def test_a_session_at_its_cap_ends_out_of_budget_and_keeps_its_candidates(
    cap: str,
    value: int,
    output_chars: int,
    owner: tuple[User, int],
    engine: Engine,
    step_log: StepLogStore,
    trees: TreeStore,
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """At the proposer's tool-call cap or the prompt cap, the session asks the model no more; its candidate still runs.

    Args:
        cap: The cap that the case sets.
        value: The cap's value.
        output_chars: The length of the extra stdout and stderr that the scripted command prints.
        owner: Alice and her budget generation.
        engine: The database.
        step_log: The step log.
        trees: The tree store.
        tmp_path: Pytest's temporary folder.
        monkeypatch: Pytest's monkeypatch.
    """
    options = _options()
    if cap == "max_tool_calls":
        options = _options(max_tool_calls=value)
    else:
        monkeypatch.setattr(loop_tools, cap, value)
    model = _Proposer(output_chars=output_chars)
    loop, _ = _loop(owner, engine, step_log, trees, tmp_path, model, generation=1)

    result = _run(loop, native_options=options)

    assert (result.best_candidate, result.best_score) == ("seedxx", 6.0)
    assert len(model.calls) == 2
    assert [kind for _, kind, _ in _steps(engine)] == ["model", "command"] * 2


def test_a_hebrew_transcript_past_the_byte_cap_ends_the_session_and_the_run_goes_on(
    owner: tuple[User, int],
    engine: Engine,
    step_log: StepLogStore,
    trees: TreeStore,
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """The prompt cap counts UTF-8 bytes, so Hebrew output under the cap in characters still ends the session.

    Each session writes its candidate and prints 24,000 Hebrew letters: under the cap as characters, past it as
    bytes. The session asks the model no more, and its candidate still runs.

    Args:
        owner: Alice and her budget generation.
        engine: The database.
        step_log: The step log.
        trees: The tree store.
        tmp_path: Pytest's temporary folder.
        monkeypatch: Pytest's monkeypatch.
    """
    monkeypatch.setattr(loop_tools, "MAX_PROMPT_BYTES", 40_000)
    model = _Proposer(output_chars=12_000, output_letter="\\u05d0")
    loop, _ = _loop(owner, engine, step_log, trees, tmp_path, model, generation=1)

    result = _run(loop)

    assert (result.best_candidate, result.best_score) == ("seedxx", 6.0)
    assert [kind for _, kind, _ in _steps(engine)] == ["model", "command"] * 2
    assert all(message["role"] != "tool" for call in model.calls for message in call)


def test_a_model_call_that_fails_after_a_command_ends_the_session_and_the_run_goes_on(
    owner: tuple[User, int], engine: Engine, step_log: StepLogStore, trees: TreeStore, tmp_path: Path
) -> None:
    """A call that fails on every attempt after the session ran a command ends it out of budget, not as an error.

    The failures are billed, so the proposer cost still counts them.

    Args:
        owner: Alice and her budget generation.
        engine: The database.
        step_log: The step log.
        trees: The tree store.
        tmp_path: Pytest's temporary folder.
    """
    model = _Proposer(billed_at=frozenset({1, 2, 3}))
    loop, _ = _loop(owner, engine, step_log, trees, tmp_path, model, generation=1)

    result = _run(loop)

    assert (result.best_candidate, result.best_score) == ("seedxx", 6.0)
    # The first session's reply, its command and three failed attempts; then the second session in full.
    first = ["model", "command", "model", "model", "model"]
    assert [kind for _, kind, _ in _steps(engine)] == [*first, "model", "command", "model"]
    # Three replies and the three billed failures, one cent each.
    assert result.metadata["proposer_cost_usd"] == 0.06


@pytest.mark.parametrize("bad", [math.inf, math.nan])
def test_a_non_finite_score_sends_no_case_event_and_no_per_case_score(
    bad: float, owner: tuple[User, int], engine: Engine, step_log: StepLogStore, trees: TreeStore, tmp_path: Path
) -> None:
    """As in the native runner, a NaN or infinite score is no score: it gets the scored mean and marks the crash.

    The crashed version then shows no score and no per-case scores in the climb view (SKY-751).

    Args:
        bad: The NaN or infinite score.
        owner: Alice and her budget generation.
        engine: The database.
        step_log: The step log.
        trees: The tree store.
        tmp_path: Pytest's temporary folder.
    """

    def score(loop: EngineLoop, candidate: Any, case: Any) -> tuple[float | None, dict[str, Any]]:
        """Score the first case as infinite, and the second by length.

        Args:
            loop: The engine loop.
            candidate: The candidate text.
            case: The case.

        Returns:
            The score and no side information.
        """
        del loop
        return (bad if case == _CASES[0] else float(len(str(candidate)))), {}

    loop, _ = _loop(owner, engine, step_log, trees, tmp_path, _Proposer(), generation=1)
    events: list[tuple[str, dict[str, Any]]] = []
    ctx = loop.context(progress_callback=lambda kind, data: events.append((kind, data)))
    adapter = meta_mod._UpstreamServer(loop.server(score, max_evals=20), list(_CASES), ctx)

    mean, info = adapter.evaluate_examples("seed")
    adapter.log_progress(4.0, "seed")

    assert mean == 4.0
    assert info["scores"] == {"0": 4.0, "1": 4.0}
    assert info[native_engines.UNSCORED_CASES_KEY] == ["0"]

    cases = [(data["example_id"], data["score"]) for kind, data in events if kind == PROGRESS_CASE_SCORED]
    assert cases == [("1", 4.0)]
    candidates = [(data["score"], data["per_example"]) for kind, data in events if kind == PROGRESS_CANDIDATE]
    assert candidates == [(None, [])]


def test_a_sweep_with_no_scored_case_lists_every_case_as_unscored(
    owner: tuple[User, int], engine: Engine, step_log: StepLogStore, trees: TreeStore, tmp_path: Path
) -> None:
    """As in the native runner, a sweep with no finite score keeps the unscored mean and lists every case.

    Args:
        owner: Alice and her budget generation.
        engine: The database.
        step_log: The step log.
        trees: The tree store.
        tmp_path: Pytest's temporary folder.
    """
    loop, _ = _loop(owner, engine, step_log, trees, tmp_path, _Proposer(), generation=1)
    scores = iter([math.nan, None])
    server = loop.server(lambda _loop, _candidate, _case: (next(scores), {}), max_evals=20)
    adapter = meta_mod._UpstreamServer(server, list(_CASES), loop.context())

    mean, info = adapter.evaluate_examples("seed")

    assert mean == float("-inf")
    assert info[native_engines.UNSCORED_CASES_KEY] == ["0", "1"]


def test_a_full_sweep_logs_its_start_as_the_native_runner_does(
    owner: tuple[User, int],
    engine: Engine,
    step_log: StepLogStore,
    trees: TreeStore,
    tmp_path: Path,
    caplog: pytest.LogCaptureFixture,
) -> None:
    """Each sweep of every case logs ``candidate.eval.start`` once, with the native payload; a partial one does not.

    Args:
        owner: Alice and her budget generation.
        engine: The database.
        step_log: The step log.
        trees: The tree store.
        tmp_path: Pytest's temporary folder.
        caplog: Pytest's log capture.
    """
    loop, _ = _loop(owner, engine, step_log, trees, tmp_path, _Proposer(), generation=1)
    adapter = meta_mod._UpstreamServer(loop.server(_score, max_evals=20), list(_CASES), loop.context())

    with caplog.at_level(logging.INFO, logger=native_engines.run_log.name):
        adapter.evaluate_examples("seed")
        adapter.evaluate_examples("seed", ["0"])

    starts = [
        (record.getMessage(), record.run_log["candidate"], record.run_log["fields"])
        for record in caplog.records
        if record.name == native_engines.run_log.name and record.run_log["event"] == "candidate.eval.start"
    ]
    assert starts == [("Evaluating candidate 0 on 2 cases", "0", {"cases": 2})]


def test_a_reply_past_the_tool_call_cap_runs_only_the_calls_under_the_cap(
    monkeypatch: pytest.MonkeyPatch,
    owner: tuple[User, int],
    engine: Engine,
    step_log: StepLogStore,
    trees: TreeStore,
    tmp_path: Path,
) -> None:
    """A reply with more tool calls than the cap allows runs the first ones, then ends the session out of budget.

    Args:
        monkeypatch: Pytest's monkeypatch.
        owner: Alice and her budget generation.
        engine: The database.
        step_log: The step log.
        trees: The tree store.
        tmp_path: Pytest's temporary folder.
    """
    loop, boxes = _loop(owner, engine, step_log, trees, tmp_path, _Proposer(), generation=1)
    worker = _worker(loop, tmp_path, loop.context())
    worker._max_tool_calls = 2
    call = {"id": "c", "name": "bash", "arguments": {"command": "true"}}
    replies = iter([ModelReply("", Decimal(1), [call, call, call])])
    monkeypatch.setattr(loop_tools, "call_model", lambda *_: (next(replies), Decimal(0)))

    outcome = worker._propose(1, None)

    assert outcome.budget_exhausted
    assert boxes.opened == 2


def test_the_transcript_holds_tool_calls_in_the_openai_form(
    owner: tuple[User, int], engine: Engine, step_log: StepLogStore, trees: TreeStore, tmp_path: Path
) -> None:
    """The next prompt holds each tool call as an OpenAI function call, with its arguments as sorted JSON text.

    Args:
        owner: Alice and her budget generation.
        engine: The database.
        step_log: The step log.
        trees: The tree store.
        tmp_path: Pytest's temporary folder.
    """
    model = _Proposer()
    loop, _ = _loop(owner, engine, step_log, trees, tmp_path, model, generation=1)

    _run(loop)

    assistant = model.calls[1][-2]
    command = assistant["tool_calls"][0]["function"]["arguments"]
    assert assistant["role"] == "assistant"
    assert assistant["tool_calls"] == [
        {"id": "call-1", "type": "function", "function": {"name": "bash", "arguments": command}}
    ]
    assert json.loads(command)["command"].startswith("printf %s seedx > agents/c1.txt")
    assert meta_mod._openai_call({"id": "i", "name": "bash", "arguments": {"b": 1, "a": 2}}) == {
        "id": "i",
        "type": "function",
        "function": {"name": "bash", "arguments": '{"a": 2, "b": 1}'},
    }
    assert meta_mod._openai_call({"id": "i", "name": "bash", "arguments": "not json"})["function"]["arguments"] == (
        "not json"
    )
    assert meta_mod._openai_call({"id": "i", "name": "bash", "arguments": {"a": "א"}})["function"]["arguments"] == (
        '{"a": "א"}'
    )


def test_a_best_version_with_no_finite_mean_has_no_score(
    owner: tuple[User, int], engine: Engine, step_log: StepLogStore, trees: TreeStore, tmp_path: Path
) -> None:
    """The seed scores NaN on every case and no rival comes: the result keeps the seed with no score, not -inf.

    Args:
        owner: Alice and her budget generation.
        engine: The database.
        step_log: The step log.
        trees: The tree store.
        tmp_path: Pytest's temporary folder.
    """
    model = _Proposer(call={"id": "x", "name": "edit", "arguments": "{}"})
    loop, _ = _loop(owner, engine, step_log, trees, tmp_path, model, generation=1)

    result = _run(loop, scorer=lambda _loop, _candidate, _case: (math.nan, {}))

    assert result.best_candidate == "seed"
    assert result.best_score is None


def test_a_best_version_with_a_crashed_case_has_no_score(
    owner: tuple[User, int], engine: Engine, step_log: StepLogStore, trees: TreeStore, tmp_path: Path
) -> None:
    """The seed crashes on one case and no rival comes: its filled mean is no score, as the server says.

    Args:
        owner: Alice and her budget generation.
        engine: The database.
        step_log: The step log.
        trees: The tree store.
        tmp_path: Pytest's temporary folder.
    """

    def score(loop: EngineLoop, candidate: Any, case: Any) -> tuple[float | None, dict[str, Any]]:
        """Crash on the first case, and score the second by length.

        Args:
            loop: The engine loop.
            candidate: The candidate text.
            case: The case.

        Returns:
            The score and the side information.
        """
        del loop
        if case == _CASES[0]:
            return None, {"error": "ValueError: bad case"}
        return float(len(str(candidate))), {}

    model = _Proposer(call={"id": "x", "name": "edit", "arguments": "{}"})
    loop, _ = _loop(owner, engine, step_log, trees, tmp_path, model, generation=1)

    result = _run(loop, scorer=score)

    assert result.best_candidate == "seed"
    assert result.best_score is None


def test_a_pending_entry_with_a_null_byte_is_skipped_and_refused(
    owner: tuple[User, int], engine: Engine, step_log: StepLogStore, trees: TreeStore, tmp_path: Path
) -> None:
    """No file system path holds a null byte: the pull skips the entry and the check refuses it, with no error.

    Args:
        owner: Alice and her budget generation.
        engine: The database.
        step_log: The step log.
        trees: The tree store.
        tmp_path: Pytest's temporary folder.
    """
    loop, _ = _loop(owner, engine, step_log, trees, tmp_path, _Proposer(), generation=1)
    worker = _worker(loop, tmp_path)
    tree = loop.put_files(
        {"logs/run/pending_eval.json": _pending("agents/a\u0000b.txt", "agents/ok.txt"), "agents/ok.txt": b"ok"}
    )

    worker._pull(tree)

    assert (worker.work_dir / "agents" / "ok.txt").read_text() == "ok"
    assert worker._validate({"name": "c0", "file": "agents/a\u0000b.txt"}) == "invalid candidate file"


def test_a_candidate_file_in_a_symlink_loop_is_refused(
    owner: tuple[User, int], engine: Engine, step_log: StepLogStore, trees: TreeStore, tmp_path: Path
) -> None:
    """Before Python 3.13 ``resolve`` raises RuntimeError on a symlink loop: the check refuses it, with no error.

    Args:
        owner: Alice and her budget generation.
        engine: The database.
        step_log: The step log.
        trees: The tree store.
        tmp_path: Pytest's temporary folder.
    """
    loop, _ = _loop(owner, engine, step_log, trees, tmp_path, _Proposer(), generation=1)
    worker = _worker(loop, tmp_path)
    agents = worker.work_dir / "agents"
    (agents / "a.txt").symlink_to(agents / "b.txt")
    (agents / "b.txt").symlink_to(agents / "a.txt")

    assert worker._validate({"name": "c0", "file": "agents/a.txt"}) == "candidate file must exist under agents/"


def test_a_best_version_with_a_non_finite_case_has_no_score(
    owner: tuple[User, int], engine: Engine, step_log: StepLogStore, trees: TreeStore, tmp_path: Path
) -> None:
    """The seed scores NaN on one case and 4 on the other: it crashed, so neither the result nor the climb scores it.

    Args:
        owner: Alice and her budget generation.
        engine: The database.
        step_log: The step log.
        trees: The tree store.
        tmp_path: Pytest's temporary folder.
    """

    def score(loop: EngineLoop, candidate: Any, case: Any) -> tuple[float | None, dict[str, Any]]:
        """Score the first case as NaN, and the second by length.

        Args:
            loop: The engine loop.
            candidate: The candidate text.
            case: The case.

        Returns:
            The score and no side information.
        """
        del loop
        return (math.nan if case == _CASES[0] else float(len(str(candidate)))), {}

    model = _Proposer(call={"id": "x", "name": "edit", "arguments": "{}"})
    loop, _ = _loop(owner, engine, step_log, trees, tmp_path, model, generation=1)
    events: list[tuple[str, dict[str, Any]]] = []

    result = _run(loop, events=events, scorer=score)

    assert result.best_candidate == "seed"
    assert result.best_score is None
    assert [data["score"] for kind, data in events if kind == PROGRESS_CANDIDATE] == [None]


def test_a_budget_too_small_for_a_full_sweep_stops_before_it(
    owner: tuple[User, int], engine: Engine, step_log: StepLogStore, trees: TreeStore, tmp_path: Path
) -> None:
    """With one scorer call left for a two-case sweep, the run stops on the eval budget and scores no case of it.

    Args:
        owner: Alice and her budget generation.
        engine: The database.
        step_log: The step log.
        trees: The tree store.
        tmp_path: Pytest's temporary folder.
    """
    pending = json.dumps(
        {"candidates": [{"name": "c1", "file": "agents/c1.txt"}, {"name": "c2", "file": "agents/c2.txt"}]}
    )
    command = (
        "printf seedx > agents/c1.txt && printf seedxx > agents/c2.txt"
        f" && printf '%s' '{pending}' > logs/run/pending_eval.json"
    )
    model = _Proposer(call={"id": "x", "name": "bash", "arguments": {"command": command}})
    loop, _ = _loop(owner, engine, step_log, trees, tmp_path, model, generation=1)

    result = _run(loop, max_evals=5)

    assert result.metadata["stop_reason"] == "eval_budget"
    # The seed and ``c1`` use four calls; ``c2`` would need two, so it scores no case.
    assert result.total_evals == 4
    assert (result.best_candidate, result.best_score) == ("seedx", 5.0)


def test_a_scorer_budget_error_mid_sweep_stops_as_upstream_budget_exhaustion(
    owner: tuple[User, int], engine: Engine, step_log: StepLogStore, trees: TreeStore, tmp_path: Path
) -> None:
    """A budget error from the scorer becomes the upstream budget error, so the engine stops on the eval budget.

    Args:
        owner: Alice and her budget generation.
        engine: The database.
        step_log: The step log.
        trees: The tree store.
        tmp_path: Pytest's temporary folder.
    """
    loop, _ = _loop(owner, engine, step_log, trees, tmp_path, _Proposer(), generation=1)
    server = MagicMock(remaining=10, used=0)
    server.evaluate.side_effect = BudgetExhaustedError("the run budget is spent")
    adapter = meta_mod._UpstreamServer(server, list(_CASES), loop.context())

    with pytest.raises(BudgetExhausted, match="the run budget is spent"):
        adapter.evaluate_examples("seed")


def test_a_run_with_no_scorer_call_left_is_refused(
    owner: tuple[User, int], engine: Engine, step_log: StepLogStore, trees: TreeStore, tmp_path: Path
) -> None:
    """With no scorer call left, the run fails on the budget before any model call.

    Args:
        owner: Alice and her budget generation.
        engine: The database.
        step_log: The step log.
        trees: The tree store.
        tmp_path: Pytest's temporary folder.
    """
    model = _Proposer()
    loop, _ = _loop(owner, engine, step_log, trees, tmp_path, model, generation=1)

    with pytest.raises(BudgetExhaustedError):
        _run(loop, max_evals=0)
    assert model.calls == []


def test_the_workspace_tree_holds_the_empty_layout_folders(
    monkeypatch: pytest.MonkeyPatch,
    owner: tuple[User, int],
    engine: Engine,
    step_log: StepLogStore,
    trees: TreeStore,
    tmp_path: Path,
) -> None:
    """A tree holds only files, so each empty layout folder gets a marker and the agent can write into it.

    Args:
        monkeypatch: Pytest's monkeypatch.
        owner: Alice and her budget generation.
        engine: The database.
        step_log: The step log.
        trees: The tree store.
        tmp_path: Pytest's temporary folder.
    """
    loop, _ = _loop(owner, engine, step_log, trees, tmp_path, _Proposer(), generation=1)
    worker = _worker(loop, tmp_path)
    (worker.logs_dir / "reports").mkdir()
    command = "printf r > logs/run/reports/r.md && printf a > agents/a.txt"
    replies = iter(
        [
            ModelReply("", Decimal(1), [{"id": "c", "name": "bash", "arguments": {"command": command}}]),
            ModelReply("done", Decimal(1), []),
        ]
    )
    prompts: list[list[dict[str, Any]]] = []

    def call(
        ctx: EngineContext,
        messages: list[dict[str, Any]],
        tools: list[dict[str, Any]],
        spent: Decimal,
        remaining_cost: float | None,
    ) -> tuple[ModelReply, Decimal]:
        """Keep the prompt and give the next scripted reply.

        Args:
            ctx: Not used.
            messages: The session's messages.
            tools: Not used.
            spent: Not used.
            remaining_cost: Not used.

        Returns:
            The reply and no failed cost.
        """
        del ctx, tools, spent, remaining_cost
        prompts.append(list(messages))
        return next(replies), Decimal(0)

    monkeypatch.setattr(loop_tools, "call_model", call)

    worker._propose(1, None)

    assert json.loads(prompts[1][-1]["content"])["exit_code"] == 0


@pytest.mark.parametrize("cap", [None, 0.02])
def test_a_billed_failed_call_counts_in_the_proposer_cost_live_and_on_a_resume(
    cap: float | None,
    owner: tuple[User, int],
    engine: Engine,
    step_log: StepLogStore,
    trees: TreeStore,
    tmp_path: Path,
) -> None:
    """A failed call that the provider billed adds its committed cost to the total and to the cap, also on a replay.

    Args:
        cap: The proposer cost cap, in dollars, or ``None`` for no cap.
        owner: Alice and her budget generation.
        engine: The database.
        step_log: The step log.
        trees: The tree store.
        tmp_path: Pytest's temporary folder.
    """
    options = _options(max_token_cost=cap)
    first, _ = _loop(owner, engine, step_log, trees, tmp_path, _Proposer(billed_at=frozenset({0})), generation=1)
    live = _run(first, native_options=options)

    _new_generation(engine)
    model = _Proposer()
    loop, _ = _loop(owner, engine, step_log, trees, tmp_path, model, generation=2)
    replayed = _run(loop, native_options=options)

    assert model.calls == []
    if cap is None:
        # Four replies of one cent each and the billed failure of one cent.
        assert live.metadata["proposer_cost_usd"] == replayed.metadata["proposer_cost_usd"] == 0.05
    else:
        # The billed failure and the first reply reach the cap, so the session asks the model no more.
        assert [kind for _, kind, _ in _steps(engine)] == ["model", "model", "command"]
        assert live.metadata["proposer_cost_usd"] == replayed.metadata["proposer_cost_usd"] == 0.02
        assert live.metadata["stop_reason"] == replayed.metadata["stop_reason"] == "proposer_budget"


def test_a_pending_entry_with_an_over_long_file_name_is_refused(
    owner: tuple[User, int], engine: Engine, step_log: StepLogStore, trees: TreeStore, tmp_path: Path
) -> None:
    """A file name past the file system's limit makes the path check fail; the run refuses the entry and goes on.

    Args:
        owner: Alice and her budget generation.
        engine: The database.
        step_log: The step log.
        trees: The tree store.
        tmp_path: Pytest's temporary folder.
    """
    file = "agents/" + "a" * 300 + ".txt"
    pending = json.dumps({"candidates": [{"name": "long", "file": file}]})
    command = f"printf '%s' '{pending}' > logs/run/pending_eval.json"
    model = _Proposer(call={"id": "x", "name": "bash", "arguments": {"command": command}})
    loop, _ = _loop(owner, engine, step_log, trees, tmp_path, model, generation=1)

    result = _run(loop)

    assert result.best_candidate == "seed"
    rows = [
        json.loads(line)
        for summary in tmp_path.rglob("evolution_summary.jsonl")
        for line in summary.read_text().splitlines()
    ]
    assert [row["outcome"] for row in rows] == ["failed: candidate file must exist under agents/"] * 2


def test_a_deeply_nested_pending_file_gives_no_candidate(
    owner: tuple[User, int], engine: Engine, step_log: StepLogStore, trees: TreeStore, tmp_path: Path
) -> None:
    """JSON nested past the parser's recursion limit is a malformed pending file: the run goes on with no candidate.

    Args:
        owner: Alice and her budget generation.
        engine: The database.
        step_log: The step log.
        trees: The tree store.
        tmp_path: Pytest's temporary folder.
    """
    command = "python3 -c \"print('[' * 200000)\" > logs/run/pending_eval.json"
    model = _Proposer(call={"id": "x", "name": "bash", "arguments": {"command": command}})
    loop, _ = _loop(owner, engine, step_log, trees, tmp_path, model, generation=1)

    result = _run(loop)

    assert (result.best_candidate, result.best_score) == ("seed", 4.0)
    assert len(model.calls) == 4


def test_a_budget_smaller_than_one_sweep_after_the_seed_stops_before_the_proposer(
    owner: tuple[User, int], engine: Engine, step_log: StepLogStore, trees: TreeStore, tmp_path: Path
) -> None:
    """After the seed's two-case sweep, one scorer call is left: the run stops on the eval budget with no model call.

    Args:
        owner: Alice and her budget generation.
        engine: The database.
        step_log: The step log.
        trees: The tree store.
        tmp_path: Pytest's temporary folder.
    """
    model = _Proposer()
    loop, _ = _loop(owner, engine, step_log, trees, tmp_path, model, generation=1)

    result = _run(loop, max_evals=3)

    assert result.metadata["stop_reason"] == "eval_budget"
    assert model.calls == []


def _nested(depth: int) -> dict[str, Any]:
    """Build a bash call's arguments nested to a depth, with no recursion.

    Args:
        depth: The number of nested objects.

    Returns:
        ``{"command": "true", "a": {"a": ...}}``, nested ``depth`` levels.
    """
    arguments: dict[str, Any] = {}
    for _ in range(depth - 1):
        arguments = {"a": arguments}
    return {"command": "true", "a": arguments}


@pytest.mark.parametrize(
    ("arguments", "error"),
    [
        ("[" * 990, "command string"),
        (json.dumps({"command": "true", "pad": "x" * meta_mod.MAX_ARGUMENT_CHARS}), "nested at most"),
        # Long JSON text whose parsed object is small: only the text bound refuses it.
        ('{"command": "true"}' + " " * meta_mod.MAX_ARGUMENT_CHARS, "nested at most"),
        ({"command": "x" * (meta_mod.MAX_ARGUMENT_CHARS + 1)}, "nested at most"),
        ({"command": "true", "pad": "x" * meta_mod.MAX_ARGUMENT_CHARS}, "nested at most"),
        # Close to the depth past which the engine loop cannot commit the reply. How deep ``json.dumps`` can go
        # depends on the stack at the call, so the transcript must not depend on it.
        (_nested(900), "nested at most"),
    ],
    ids=["deep-text", "long-text", "padded-text", "long-command", "long-object", "deep-object"],
)
def test_an_over_long_or_deeply_nested_argument_gets_an_error_result_and_the_run_goes_on(
    arguments: Any,
    error: str,
    owner: tuple[User, int],
    engine: Engine,
    step_log: StepLogStore,
    trees: TreeStore,
    tmp_path: Path,
) -> None:
    """A model reply with arguments past the bounds gets an error result and opens no box; a resume replays it.

    Args:
        arguments: The tool call's arguments.
        error: Text that the error result holds.
        owner: Alice and her budget generation.
        engine: The database.
        step_log: The step log.
        trees: The tree store.
        tmp_path: Pytest's temporary folder.
    """
    model = _Proposer(call={"id": "x", "name": "bash", "arguments": arguments})
    loop, boxes = _loop(owner, engine, step_log, trees, tmp_path, model, generation=1)

    result = _run(loop)

    assert result.best_candidate == "seed"
    assert boxes.opened == 0
    assert error in json.loads(model.calls[1][-1]["content"])["error"]
    refused = isinstance(arguments, dict) or len(arguments) > meta_mod.MAX_ARGUMENT_CHARS
    assert model.calls[1][-2]["tool_calls"][0]["function"]["arguments"] == ("{}" if refused else arguments)
    resumed, _ = _loop(owner, engine, step_log, trees, tmp_path, _Proposer(die_at=0), generation=2)
    assert _run(resumed).best_candidate == "seed"


def test_an_argument_at_the_depth_bound_still_runs(
    owner: tuple[User, int], engine: Engine, step_log: StepLogStore, trees: TreeStore, tmp_path: Path
) -> None:
    """Arguments nested exactly ``MAX_ARGUMENT_DEPTH`` levels run their command, and the transcript keeps them.

    Args:
        owner: Alice and her budget generation.
        engine: The database.
        step_log: The step log.
        trees: The tree store.
        tmp_path: Pytest's temporary folder.
    """
    loop, _ = _loop(owner, engine, step_log, trees, tmp_path, _Proposer(), generation=1)
    worker = _worker(loop, tmp_path)
    tree = loop.put_files({"a.txt": b"a"})
    arguments = _nested(meta_mod.MAX_ARGUMENT_DEPTH - 1)

    output, _ = worker._exec(tree, {"id": "c", "name": "bash", "arguments": arguments})

    assert json.loads(output)["exit_code"] == 0
    assert (
        json.loads(meta_mod._openai_call({"id": "c", "name": "bash", "arguments": arguments})["function"]["arguments"])
        == arguments
    )
    deeper = _nested(meta_mod.MAX_ARGUMENT_DEPTH)
    assert "error" in json.loads(worker._exec(tree, {"id": "c", "name": "bash", "arguments": deeper})[0])


def test_a_pending_file_over_its_cap_gives_no_candidate(
    monkeypatch: pytest.MonkeyPatch,
    owner: tuple[User, int],
    engine: Engine,
    step_log: StepLogStore,
    trees: TreeStore,
    tmp_path: Path,
) -> None:
    """A ``pending_eval.json`` past ``MAX_PENDING_BYTES`` is malformed, in the tree and in the work folder.

    Args:
        monkeypatch: Pytest's monkeypatch.
        owner: Alice and her budget generation.
        engine: The database.
        step_log: The step log.
        trees: The tree store.
        tmp_path: Pytest's temporary folder.
    """
    loop, _ = _loop(owner, engine, step_log, trees, tmp_path, _Proposer(), generation=1)
    worker = _worker(loop, tmp_path)
    pending = _pending("agents/a.txt")
    monkeypatch.setattr(native_engines, "MAX_PENDING_BYTES", len(pending) - 1)
    tree = loop.put_files({"logs/run/pending_eval.json": pending, "agents/a.txt": b"a"})

    worker._pull(tree)

    assert (worker.logs_dir / "pending_eval.json").read_bytes() == b""
    assert not (worker.work_dir / "agents" / "a.txt").exists()
    local = worker.logs_dir / "local_pending.json"
    local.write_bytes(pending)
    assert worker._read_pending(local) == []
    monkeypatch.setattr(native_engines, "MAX_PENDING_BYTES", len(pending))
    assert worker._read_pending(local) == [{"name": "c0", "file": "agents/a.txt"}]


def test_a_candidate_file_over_its_cap_is_refused_with_a_failed_row(
    monkeypatch: pytest.MonkeyPatch,
    owner: tuple[User, int],
    engine: Engine,
    step_log: StepLogStore,
    trees: TreeStore,
    tmp_path: Path,
) -> None:
    """A candidate file past ``MAX_CANDIDATE_BYTES`` gets no local copy and gets a failed row for its size.

    Args:
        monkeypatch: Pytest's monkeypatch.
        owner: Alice and her budget generation.
        engine: The database.
        step_log: The step log.
        trees: The tree store.
        tmp_path: Pytest's temporary folder.
    """
    monkeypatch.setattr(native_engines, "MAX_CANDIDATE_BYTES", 100)
    pending = json.dumps({"candidates": [{"name": "big", "file": "agents/big.txt"}]})
    command = (
        f"python3 -c \"print('x' * 500)\" > agents/big.txt && printf '%s' '{pending}' > logs/run/pending_eval.json"
    )
    model = _Proposer(call={"id": "x", "name": "bash", "arguments": {"command": command}})
    loop, _ = _loop(owner, engine, step_log, trees, tmp_path, model, generation=1)

    result = _run(loop)

    assert result.best_candidate == "seed"
    rows = [
        json.loads(line)
        for summary in tmp_path.rglob("evolution_summary.jsonl")
        for line in summary.read_text().splitlines()
    ]
    assert [row["outcome"] for row in rows] == ["failed: candidate file is larger than a version may be"] * 2
    assert list(tmp_path.rglob("agents/big.txt")) == []


def test_an_over_cap_candidate_file_replaces_no_stale_local_copy_and_is_refused_by_size(
    monkeypatch: pytest.MonkeyPatch,
    owner: tuple[User, int],
    engine: Engine,
    step_log: StepLogStore,
    trees: TreeStore,
    tmp_path: Path,
) -> None:
    """An over-cap file in the tree removes the local copy of an earlier pull, and ``_validate`` names its size.

    Args:
        monkeypatch: Pytest's monkeypatch.
        owner: Alice and her budget generation.
        engine: The database.
        step_log: The step log.
        trees: The tree store.
        tmp_path: Pytest's temporary folder.
    """
    monkeypatch.setattr(native_engines, "MAX_CANDIDATE_BYTES", 10)
    loop, _ = _loop(owner, engine, step_log, trees, tmp_path, _Proposer(), generation=1)
    worker = _worker(loop, tmp_path)
    (worker.work_dir / "agents" / "big.txt").write_text("stale")
    tree = loop.put_files({"logs/run/pending_eval.json": _pending("agents/big.txt"), "agents/big.txt": b"x" * 11})

    worker._pull(tree)

    assert not (worker.work_dir / "agents" / "big.txt").exists()
    assert worker._validate({"name": "c0", "file": "agents/./big.txt"}) == native_engines.CANDIDATE_TOO_LARGE
    assert worker._validate({"name": "c1", "file": "agents/other.txt"}) == native_engines.CANDIDATE_MISSING


def _pack_reads(monkeypatch: pytest.MonkeyPatch) -> list[bytes]:
    """Record the bytes of each pack range read, the read that loads a flushed object's bytes.

    The cap check in the step log runs before this read, so a refused object never shows here.

    Args:
        monkeypatch: Pytest's monkeypatch; it undoes the wrap after the test.

    Returns:
        The list that gets the bytes of each range read.
    """
    loaded: list[bytes] = []
    get = LocalObjectStore.get

    def ranged(store: LocalObjectStore, key: str, offset: int = 0, length: int | None = None) -> bytes:
        """Record the bytes of one range read.

        Args:
            store: The object store.
            key: The pack key.
            offset: The first byte.
            length: The bytes to read; ``None`` reads to the end.

        Returns:
            The bytes of the range.
        """
        data = get(store, key, offset, length)
        loaded.append(data)
        return data

    monkeypatch.setattr(LocalObjectStore, "get", ranged)
    return loaded


def test_an_over_cap_candidate_file_never_loads_and_is_refused_by_size(
    monkeypatch: pytest.MonkeyPatch,
    owner: tuple[User, int],
    engine: Engine,
    step_log: StepLogStore,
    trees: TreeStore,
    tmp_path: Path,
) -> None:
    """``_pull`` reads an over-cap candidate file's size and never its bytes, and ``_validate`` names its size.

    Args:
        monkeypatch: Pytest's monkeypatch.
        owner: Alice and her budget generation.
        engine: The database.
        step_log: The step log.
        trees: The tree store.
        tmp_path: Pytest's temporary folder.
    """
    monkeypatch.setattr(native_engines, "MAX_CANDIDATE_BYTES", 10)
    loop, _ = _loop(owner, engine, step_log, trees, tmp_path, _Proposer(), generation=1)
    worker = _worker(loop, tmp_path)
    big = b"x" * 5000
    tree = loop.put_files({"logs/run/pending_eval.json": _pending("agents/big.txt"), "agents/big.txt": big})
    step_log.flush()
    loaded = _pack_reads(monkeypatch)

    worker._pull(tree)

    assert loaded
    assert not any(big in data for data in loaded)
    assert not (worker.work_dir / "agents" / "big.txt").exists()
    assert worker._validate({"name": "c0", "file": "agents/big.txt"}) == native_engines.CANDIDATE_TOO_LARGE


def test_an_over_cap_pending_file_never_loads_and_gives_no_candidate(
    monkeypatch: pytest.MonkeyPatch,
    owner: tuple[User, int],
    engine: Engine,
    step_log: StepLogStore,
    trees: TreeStore,
    tmp_path: Path,
) -> None:
    """``_pull`` reads an over-cap ``pending_eval.json``'s size and never its bytes, and gives no candidate.

    Args:
        monkeypatch: Pytest's monkeypatch.
        owner: Alice and her budget generation.
        engine: The database.
        step_log: The step log.
        trees: The tree store.
        tmp_path: Pytest's temporary folder.
    """
    loop, _ = _loop(owner, engine, step_log, trees, tmp_path, _Proposer(), generation=1)
    worker = _worker(loop, tmp_path)
    pending = _pending("agents/a.txt")
    monkeypatch.setattr(native_engines, "MAX_PENDING_BYTES", len(pending) - 1)
    tree = loop.put_files({"logs/run/pending_eval.json": pending, "agents/a.txt": b"a"})
    step_log.flush()
    loaded = _pack_reads(monkeypatch)

    worker._pull(tree)

    assert loaded
    assert not any(pending in data for data in loaded)
    assert (worker.logs_dir / "pending_eval.json").read_bytes() == b""
    assert worker._read_pending(worker.logs_dir / "pending_eval.json") == []
    assert not (worker.work_dir / "agents" / "a.txt").exists()


def test_a_later_session_sees_the_tree_and_not_the_pulled_copies(
    monkeypatch: pytest.MonkeyPatch,
    owner: tuple[User, int],
    engine: Engine,
    step_log: StepLogStore,
    trees: TreeStore,
    tmp_path: Path,
) -> None:
    """A pulled candidate that the agent edits or deletes, and an over-cap one, keep their tree state next session.

    Args:
        monkeypatch: Pytest's monkeypatch.
        owner: Alice and her budget generation.
        engine: The database.
        step_log: The step log.
        trees: The tree store.
        tmp_path: Pytest's temporary folder.
    """
    monkeypatch.setattr(native_engines, "MAX_CANDIDATE_BYTES", 10)
    loop, _ = _loop(owner, engine, step_log, trees, tmp_path, _Proposer(), generation=1)
    worker = _worker(loop, tmp_path)
    pending = _pending("agents/a.txt", "agents/b.txt", "agents/big.txt").decode()
    commands = [
        f"printf one > agents/a.txt && printf b > agents/b.txt && printf %011d 0 > agents/big.txt"
        f" && printf '%s' '{pending}' > logs/run/pending_eval.json",
        "printf two > agents/a.txt && rm agents/b.txt",
        "cat agents/a.txt; test -e agents/b.txt && echo ' b'; echo; wc -c < agents/big.txt",
    ]
    outputs: list[str] = []

    def call(
        ctx: EngineContext,
        messages: list[dict[str, Any]],
        tools: list[dict[str, Any]],
        spent: Decimal,
        remaining_cost: float | None,
    ) -> tuple[ModelReply, Decimal]:
        """Send the session's one command, then end the session after its result.

        Args:
            ctx: Not used.
            messages: The session's messages.
            tools: Not used.
            spent: Not used.
            remaining_cost: Not used.

        Returns:
            The reply and no failed cost.
        """
        del ctx, tools, spent, remaining_cost
        if messages[-1]["role"] == "tool":
            outputs.append(json.loads(messages[-1]["content"])["stdout"])
            return ModelReply("done", Decimal(0), []), Decimal(0)
        tool_call = {"id": "c", "name": "bash", "arguments": {"command": commands.pop(0)}}
        return ModelReply("", Decimal(0), [tool_call]), Decimal(0)

    monkeypatch.setattr(loop_tools, "call_model", call)

    for iteration in (1, 2, 3):
        worker._propose(iteration, None)

    assert outputs[2].split() == ["two", "11"]


@pytest.mark.parametrize(
    ("cap", "billed_at", "kinds", "best"),
    [(0.01, 0, ["model"], "seed"), (0.02, 1, ["model", "command", "model"], "seedx")],
)
def test_a_billed_failure_at_the_proposer_cap_is_not_tried_again(
    cap: float,
    billed_at: int,
    kinds: list[str],
    best: str,
    owner: tuple[User, int],
    engine: Engine,
    step_log: StepLogStore,
    trees: TreeStore,
    tmp_path: Path,
) -> None:
    """A billed failed call that reaches the cap ends the session out of budget with no retry, live and on a resume.

    Args:
        cap: The proposer cost cap, in dollars.
        billed_at: The index of the attempt that fails after the provider billed one cent; the replies before it
            cost one cent each and count toward the cap.
        kinds: The committed step kinds.
        best: The run's best candidate.
        owner: Alice and her budget generation.
        engine: The database.
        step_log: The step log.
        trees: The tree store.
        tmp_path: Pytest's temporary folder.
    """
    options = _options(max_token_cost=cap)
    model = _Proposer(billed_at=frozenset({billed_at}))
    first, _ = _loop(owner, engine, step_log, trees, tmp_path, model, generation=1)
    live = _run(first, native_options=options)

    _new_generation(engine)
    again = _Proposer()
    loop, _ = _loop(owner, engine, step_log, trees, tmp_path, again, generation=2)
    replayed = _run(loop, native_options=options)

    assert model.attempts == billed_at + 1
    assert again.calls == []
    assert [kind for _, kind, _ in _steps(engine)] == kinds
    assert live.best_candidate == replayed.best_candidate == best
    assert live.metadata["proposer_cost_usd"] == replayed.metadata["proposer_cost_usd"] == cap
    assert live.metadata["stop_reason"] == replayed.metadata["stop_reason"] == "proposer_budget"


@pytest.mark.parametrize(
    ("command", "runs"),
    [
        (": " + "x" * (loop_tools.MAX_COMMAND_BYTES - 2), True),
        (": " + "x" * (loop_tools.MAX_COMMAND_BYTES - 1), False),
        # One two-byte character: the command is at the bound in characters, and one byte over it as UTF-8.
        (": " + "x" * (loop_tools.MAX_COMMAND_BYTES - 4) + "é", True),
        (": " + "x" * (loop_tools.MAX_COMMAND_BYTES - 3) + "é", False),
        # Hebrew letters are two bytes each, and JSON escapes them as six characters each, past MAX_ARGUMENT_CHARS.
        (": " + "א" * ((loop_tools.MAX_COMMAND_BYTES - 2) // 2), True),
    ],
    ids=["ascii-at-bound", "ascii-over", "utf8-at-bound", "utf8-over", "hebrew-at-bound"],
)
def test_a_command_over_its_byte_bound_gets_an_error_result(
    command: str,
    runs: bool,
    owner: tuple[User, int],
    engine: Engine,
    step_log: StepLogStore,
    trees: TreeStore,
    tmp_path: Path,
) -> None:
    """A command up to ``MAX_COMMAND_BYTES`` UTF-8 bytes runs and the transcript keeps it; a longer one gets an
    error result and opens no box.

    Args:
        command: The bash command.
        runs: Whether the command gets to the box.
        owner: Alice and her budget generation.
        engine: The database.
        step_log: The step log.
        trees: The tree store.
        tmp_path: Pytest's temporary folder.
    """
    loop, boxes = _loop(owner, engine, step_log, trees, tmp_path, _Proposer(), generation=1)
    worker = _worker(loop, tmp_path)
    tree = loop.put_files({"a.txt": b"a"})

    output, after = worker._exec(tree, {"id": "c", "name": "bash", "arguments": {"command": command}})

    if runs:
        assert json.loads(output)["exit_code"] == 0
        sent = meta_mod._openai_call({"id": "c", "name": "bash", "arguments": {"command": command}})
        assert json.loads(sent["function"]["arguments"]) == {"command": command}
    else:
        assert f"at most {loop_tools.MAX_COMMAND_BYTES} bytes" in json.loads(output)["error"]
        assert after == tree
        assert boxes.opened == 0


@pytest.mark.parametrize("form", ["absolute", "dot-dot"])
def test_a_candidate_path_that_is_not_relative_under_agents_is_refused(
    form: str, owner: tuple[User, int], engine: Engine, step_log: StepLogStore, trees: TreeStore, tmp_path: Path
) -> None:
    """A path into ``agents/`` that ``_pull`` does not copy is refused, so no stale local copy gets benchmarked.

    Args:
        form: How the entry spells the path into ``agents/``.
        owner: Alice and her budget generation.
        engine: The database.
        step_log: The step log.
        trees: The tree store.
        tmp_path: Pytest's temporary folder.
    """
    loop, _ = _loop(owner, engine, step_log, trees, tmp_path, _Proposer(), generation=1)
    worker = _worker(loop, tmp_path)
    stale = worker.work_dir / "agents" / "c1.txt"
    stale.write_text("stale")
    file = str(stale) if form == "absolute" else f"../{worker.work_dir.name}/agents/c1.txt"
    tree = loop.put_files({"logs/run/pending_eval.json": _pending(file), "agents/c1.txt": b"tree"})

    worker._pull(tree)

    assert stale.read_text() == "stale"
    assert worker._validate({"name": "c1", "file": file}) == meta_mod.CANDIDATE_OUTSIDE_AGENTS
    assert worker._validate({"name": "c1", "file": "agents/c1.txt"}) is None


def test_a_session_that_writes_no_pending_file_adds_no_rows(
    owner: tuple[User, int], engine: Engine, step_log: StepLogStore, trees: TreeStore, tmp_path: Path
) -> None:
    """A session that writes no ``pending_eval.json`` gets no candidate, not the last session's candidates again.

    Args:
        owner: Alice and her budget generation.
        engine: The database.
        step_log: The step log.
        trees: The tree store.
        tmp_path: Pytest's temporary folder.
    """
    model = _Proposer()

    def second_writes_nothing(prompt: list[dict[str, Any]], tools: list[dict[str, Any]] | None) -> ModelReply:
        """Answer as the scripted agent, but run ``true`` in the second session.

        Args:
            prompt: The session's messages.
            tools: The tool definitions.

        Returns:
            The model's reply.
        """
        user = next(message["content"] for message in prompt if message["role"] == "user")
        if "Run iteration 2" in user and prompt[-1]["role"] != "tool":
            return ModelReply("", Decimal(1), [{"id": "t", "name": "bash", "arguments": {"command": "true"}}])
        return model(prompt, tools)

    loop, _ = _loop(owner, engine, step_log, trees, tmp_path, second_writes_nothing, generation=1)

    result = _run(loop)

    rows = [
        json.loads(line)
        for summary in tmp_path.rglob("evolution_summary.jsonl")
        for line in summary.read_text().splitlines()
    ]
    assert [row["iteration"] for row in rows] == [1]
    assert not rows[0]["outcome"].startswith("failed")
    assert result.best_candidate == "seedx"


def test_a_file_over_its_cap_in_one_pull_is_missing_in_the_next(
    monkeypatch: pytest.MonkeyPatch,
    owner: tuple[User, int],
    engine: Engine,
    step_log: StepLogStore,
    trees: TreeStore,
    tmp_path: Path,
) -> None:
    """Each pull starts a new over-cap set, so a file that left the tree gets the missing reason, not the size one.

    Args:
        monkeypatch: Pytest's monkeypatch.
        owner: Alice and her budget generation.
        engine: The database.
        step_log: The step log.
        trees: The tree store.
        tmp_path: Pytest's temporary folder.
    """
    monkeypatch.setattr(native_engines, "MAX_CANDIDATE_BYTES", 10)
    loop, _ = _loop(owner, engine, step_log, trees, tmp_path, _Proposer(), generation=1)
    worker = _worker(loop, tmp_path)
    pending = _pending("agents/big.txt")
    worker._pull(loop.put_files({"logs/run/pending_eval.json": pending, "agents/big.txt": b"x" * 11}))
    assert worker._validate({"name": "c0", "file": "agents/big.txt"}) == native_engines.CANDIDATE_TOO_LARGE

    worker._pull(loop.put_files({"logs/run/pending_eval.json": pending}))

    assert worker._validate({"name": "c0", "file": "agents/big.txt"}) == native_engines.CANDIDATE_MISSING
