"""Tests for AutoResearch in the worker engine loop (SKY-725).

The tests use the real engine loop over a SQLite file, local subprocess boxes,
a scripted fake model and a pure Python scorer: no network and no paid call.
"""

from __future__ import annotations

import dataclasses
import errno
import functools
import json
import logging
import math
import os
import re
import sys
from collections.abc import Callable
from decimal import Decimal
from pathlib import Path
from types import SimpleNamespace
from typing import Any
from unittest.mock import MagicMock

import pytest
from gepa.oa.config import OptimizeAnythingConfig
from sqlalchemy.engine import Engine

from core.billing.model_gateway import MCP_RELAY_ROUTE_KEY
from core.constants import PROGRESS_CANDIDATE, PROGRESS_CASE_SCORED, PROGRESS_MINIBATCH
from core.exceptions import ServiceError
from core.models.blackbox import BLACKBOX_HARNESS_CUSTOM, BLACKBOX_HARNESS_PI, BlackboxProposer
from core.service_gateway.optimization.blackbox import autoresearch as ar_mod
from core.service_gateway.optimization.blackbox import loop_tools, native_engines
from core.service_gateway.optimization.blackbox.autoresearch import BASH_TOOL, EVALUATE_TOOL, AutoResearchEngine
from core.service_gateway.optimization.blackbox.native_engines import ProposerFailedError
from core.service_gateway.optimization.blackbox.protocol import (
    EngineContext,
    EvalServer,
    FileTooLargeError,
    ModelReply,
    RepoSeed,
    Result,
    Task,
)
from core.storage.step_log_store import StepLogStore, step_id_for
from core.storage.tree_store import TreeStore
from core.worker.actors import User
from core.worker.engine_loop import (
    ROOT_AGENT_ID,
    EngineLoop,
    LoopStoppedError,
    ModelCallBilledError,
    ModelCallFailedError,
)
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
# A scripted turn: the tool call, or the tool calls, to make in a round, or ``None`` for a final reply.
Turn = Callable[[int], dict[str, Any] | list[dict[str, Any]] | None]
# The model calls a test lets run; a session that never stops fails the test here, not in a hang.
_MAX_CALLS = 40


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


def _bash(command: str) -> Turn:
    """Script a bash call.

    Args:
        command: The command; ``{n}`` is the round number and ``{x}`` is ``n`` times ``x``.

    Returns:
        The turn.
    """
    return lambda n: {"id": f"bash-{n}", "name": "bash", "arguments": {"command": command.format(n=n, x="x" * n)}}


def _evaluate(**extra: Any) -> Turn:
    """Script an evaluate call on the round's candidate file.

    Args:
        **extra: Other arguments, such as ``example_ids``.

    Returns:
        The turn.
    """
    return lambda n: {"id": f"eval-{n}", "name": "evaluate", "arguments": {"file": f"work/c{n}.txt", **extra}}


_WRITE = _bash("printf %s seed{x} > work/c{n}.txt && printf '## Round {n}\\n' >> notebook.md")
_ROUND = [_WRITE, _evaluate()]


class _Researcher:
    """A scripted research agent: the same turns in every round, then a final reply."""

    def __init__(
        self,
        script: list[Turn] | None = None,
        *,
        die_at: int | None = None,
        cost: str = "1",
        rest: Turn | None = None,
        fail_at: frozenset[int] = frozenset(),
        billed_at: frozenset[int] = frozenset(),
    ) -> None:
        """Set the script.

        Args:
            script: The turns of each round; one write and one full evaluation when ``None``.
            die_at: The index of the call in which the worker dies; ``None`` never dies.
            cost: The cost of each reply, in cents.
            rest: The turn after the script ends; a final reply when ``None``.
            fail_at: The indexes of the attempts, failed ones included, that fail as a rate limit does.
            billed_at: The indexes of the attempts that fail after the provider billed one cent.
        """
        self.script = _ROUND if script is None else script
        self.die_at = die_at
        self.fail_at = fail_at
        self.billed_at = billed_at
        self.attempts = 0
        self.cost = Decimal(cost)
        self.rest = rest
        self.calls: list[list[dict[str, Any]]] = []
        self.tools: list[list[dict[str, Any]] | None] = []

    def __call__(self, prompt: str | list[dict[str, Any]], tools: list[dict[str, Any]] | None) -> ModelReply:
        """Answer one turn of a round.

        Args:
            prompt: The session's messages.
            tools: The tool definitions.

        Returns:
            The scripted tool calls of this turn, or a final reply.

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
        assert len(self.calls) < _MAX_CALLS, "the session did not stop"
        assert isinstance(prompt, list)
        self.calls.append([dict(message) for message in prompt])
        self.tools.append(tools)
        n = int(re.search(r"This is round (\d+)", prompt[0]["content"]).group(1))
        turn = sum(message["role"] == "assistant" for message in prompt)
        step = self.script[turn] if turn < len(self.script) else self.rest
        call = None if step is None else step(n)
        calls = [] if call is None else call if isinstance(call, list) else [call]
        return ModelReply("" if calls else "done", self.cost, calls)


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


def _score_a_py(loop: EngineLoop, candidate: Any, case: Any) -> tuple[float | None, dict[str, Any]]:
    """Score a version tree by the length of its ``a.py``.

    Args:
        loop: The engine loop.
        candidate: The version's tree id.
        case: Not used.

    Returns:
        The length and no side information.
    """
    del case
    return float(len(loop.read_file(candidate, "a.py") or b"")), {}


def _loop(
    owner: tuple[User, int],
    engine: Engine,
    step_log: StepLogStore,
    trees: TreeStore,
    tmp_path: Path,
    model: _Researcher,
    *,
    generation: int,
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
    )
    return loop, boxes


def _run(
    loop: EngineLoop,
    *,
    events: list[tuple[str, dict[str, Any]]] | None = None,
    cases: list[Any] | None = None,
    max_evals: int = 20,
    stop_at_score: float | None = 6.0,
    score: Callable[[EngineLoop, Any, Any], tuple[float | None, dict[str, Any]]] = _score,
    **fields: Any,
) -> Result:
    """Run AutoResearch on the loop; with the default script it stops at the second round's score.

    Args:
        loop: The engine loop.
        events: A list that collects the progress events.
        cases: The task's cases; two cases when ``None``.
        max_evals: The scorer calls of the run.
        stop_at_score: The score that ends the run.
        score: The scorer.
        **fields: Other context fields.

    Returns:
        The engine's result.
    """
    sink = None if events is None else (lambda kind, data: events.append((kind, data)))
    ctx = loop.context(stop_at_score=stop_at_score, progress_callback=sink, **fields)
    task = Task("seed", cases=list(_CASES if cases is None else cases))
    return AutoResearchEngine().run(task, loop.server(score, max_evals=max_evals), ctx)


def _options(
    *,
    max_token_cost: float | None = None,
    reasoning_effort: str | None = None,
    budget_route: Any = None,
    **proposer: Any,
) -> SimpleNamespace:
    """Build the native options that the engine reads.

    Args:
        max_token_cost: The proposer spend cap in dollars, or ``None``.
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
        repo=None,
    )


def _lab(tmp_path: Path, generation: int = 1) -> Path:
    """Return the local lab folder of a loop.

    Args:
        tmp_path: Pytest's temporary folder.
        generation: The loop's generation.

    Returns:
        The lab folder.
    """
    return tmp_path / f"loop-{generation}" / "lab"


def _tool_result(model: _Researcher, call: int) -> dict[str, Any]:
    """Return the tool result that a model call received last.

    Args:
        model: The scripted model.
        call: The index of the call.

    Returns:
        The tool result.
    """
    message = model.calls[call][-1]
    assert message["role"] == "tool"
    return json.loads(message["content"])


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
    monkeypatch.setattr(ar_mod, "run_native_engine", native)
    loop, _ = _loop(owner, engine, step_log, trees, tmp_path, _Researcher(), generation=1)

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
    monkeypatch.setattr(ar_mod, "run_native_engine", native)
    task = Task("seed")
    server = EvalServer(lambda candidate, example: (1.0, {}), max_evals=5)
    ctx = EngineContext(reflection_lm=str, run_dir=str(tmp_path))

    assert AutoResearchEngine().run(task, server, ctx) is incumbent
    native.assert_called_once_with("autoresearch", task, server, ctx)


def test_each_model_reply_and_each_command_commits_one_step(
    owner: tuple[User, int], engine: Engine, step_log: StepLogStore, trees: TreeStore, tmp_path: Path
) -> None:
    """Each round is a reply with a command, a reply with an evaluation and a final reply; scoring opens no box.

    Args:
        owner: Alice and her budget generation.
        engine: The database.
        step_log: The step log.
        trees: The tree store.
        tmp_path: Pytest's temporary folder.
    """
    model = _Researcher()
    loop, boxes = _loop(owner, engine, step_log, trees, tmp_path, model, generation=1)

    result = _run(loop)

    assert (result.best_candidate, result.best_score) == ("seedxx", 6.0)
    assert result.total_evals == 4
    assert result.metadata["rounds"] == 2
    assert result.metadata["directives"] == ["survey", "exploit"]
    assert result.metadata["session_ids"] == ["round1", "round2"]
    assert result.metadata["proposer_cost_usd"] == 0.06
    assert [kind for _, kind, _ in _steps(engine)] == ["model", "command", "model", "model"] * 2
    assert boxes.opened == 2
    assert not +boxes.open
    assert model.tools == [[BASH_TOOL, EVALUATE_TOOL]] * 6
    assert _tool_result(model, 1)["exit_code"] == 0
    assert _tool_result(model, 2)["average_score"] == 5.0


def test_a_run_killed_mid_round_resumes_from_its_last_step(
    owner: tuple[User, int], engine: Engine, step_log: StepLogStore, trees: TreeStore, tmp_path: Path
) -> None:
    """Kill the worker in round two's second model call, then resume: no committed reply or command runs again.

    Args:
        owner: Alice and her budget generation.
        engine: The database.
        step_log: The step log.
        trees: The tree store.
        tmp_path: Pytest's temporary folder.
    """
    first, _ = _loop(owner, engine, step_log, trees, tmp_path, _Researcher(die_at=4), generation=1)
    with pytest.raises(_WorkerDiedError):
        _run(first)
    assert [kind for _, kind, _ in _steps(engine)] == ["model", "command", "model", "model", "model", "command"]

    _new_generation(engine)
    model = _Researcher()
    loop, boxes = _loop(owner, engine, step_log, trees, tmp_path, model, generation=2)
    result = _run(loop)

    assert (result.best_candidate, result.best_score) == ("seedxx", 6.0)
    assert result.metadata["directives"] == ["survey", "exploit"]
    # Round one and round two's command come back from the log; only the last two replies call the model.
    assert len(model.calls) == 2
    assert boxes.opened == 0
    steps = _steps(engine)
    assert [kind for _, kind, _ in steps] == ["model", "command", "model", "model"] * 2
    assert [generation for _, _, generation in steps] == [1] * 6 + [2] * 2
    # The replayed rounds rebuilt the agent's notebook from the committed trees.
    assert "## Round 1\n## Round 2\n" in (_lab(tmp_path, 2) / "notebook.md").read_text()


def test_the_notebook_and_work_carry_over_to_the_next_round(
    owner: tuple[User, int], engine: Engine, step_log: StepLogStore, trees: TreeStore, tmp_path: Path
) -> None:
    """The lab is a new tree each round, so the agent's files must come back from the tree after a round.

    Args:
        owner: Alice and her budget generation.
        engine: The database.
        step_log: The step log.
        trees: The tree store.
        tmp_path: Pytest's temporary folder.
    """
    model = _Researcher()
    loop, _ = _loop(owner, engine, step_log, trees, tmp_path, model, generation=1)

    _run(loop)

    lab = _lab(tmp_path)
    assert "## Round 1\n## Round 2\n" in (lab / "notebook.md").read_text()
    assert sorted(path.name for path in (lab / "work").iterdir()) == ["c1.txt", "c2.txt", "seed.txt"]
    assert not (lab / "eval.sh").exists()


def test_the_climb_view_gets_events_only_for_full_sweeps(
    owner: tuple[User, int], engine: Engine, step_log: StepLogStore, trees: TreeStore, tmp_path: Path
) -> None:
    """A probe scores and spends budget but streams no case; each full sweep streams its cases and one candidate.

    Args:
        owner: Alice and her budget generation.
        engine: The database.
        step_log: The step log.
        trees: The tree store.
        tmp_path: Pytest's temporary folder.
    """
    model = _Researcher([_WRITE, _evaluate(example_ids=["0"]), _evaluate()])
    loop, _ = _loop(owner, engine, step_log, trees, tmp_path, model, generation=1)
    events: list[tuple[str, dict[str, Any]]] = []

    result = _run(loop, events=events)

    assert result.total_evals == 6
    probe = _tool_result(model, 2)
    assert (probe["scores"], probe["budget"]) == ({"0": 5.0}, {"used": 1, "remaining": 19})
    cases = [
        (data["trial"], data["example_id"], data["score"]) for kind, data in events if kind == PROGRESS_CASE_SCORED
    ]
    assert cases == [(0, "0", 5.0), (0, "1", 5.0), (1, "0", 6.0), (1, "1", 6.0)]
    candidates = [data for kind, data in events if kind == PROGRESS_CANDIDATE]
    assert [(data["candidate_id"], data["score"], data["per_example"]) for data in candidates] == [
        ("0", 5.0, [{"id": "0", "score": 5.0}, {"id": "1", "score": 5.0}]),
        ("1", 6.0, [{"id": "0", "score": 6.0}, {"id": "1", "score": 6.0}]),
    ]


def test_a_refused_sweep_answers_the_marker_and_a_round_with_no_evaluation_ends_the_run(
    owner: tuple[User, int], engine: Engine, step_log: StepLogStore, trees: TreeStore, tmp_path: Path
) -> None:
    """With one scorer call left for two cases, round two's sweep gets the marker; the round scored nothing.

    Args:
        owner: Alice and her budget generation.
        engine: The database.
        step_log: The step log.
        trees: The tree store.
        tmp_path: Pytest's temporary folder.
    """
    model = _Researcher()
    loop, _ = _loop(owner, engine, step_log, trees, tmp_path, model, generation=1)

    result = _run(loop, max_evals=3, stop_at_score=None)

    assert (result.best_candidate, result.best_score) == ("seedx", 5.0)
    assert result.metadata["rounds"] == 2
    assert result.total_evals == 2
    assert _tool_result(model, 5) == {"error": native_engines.BUDGET_EXHAUSTED_MARKER}


def test_a_partly_refused_sweep_lets_another_round_spend_the_rest(
    owner: tuple[User, int], engine: Engine, step_log: StepLogStore, trees: TreeStore, tmp_path: Path
) -> None:
    """Round one's last sweep is refused with one unit left; round two starts and spends it on a probe.

    Args:
        owner: Alice and her budget generation.
        engine: The database.
        step_log: The step log.
        trees: The tree store.
        tmp_path: Pytest's temporary folder.
    """
    model = _Researcher([_WRITE, _evaluate(), _evaluate(example_ids=["0"]), _evaluate()])
    loop, _ = _loop(owner, engine, step_log, trees, tmp_path, model, generation=1)

    result = _run(loop, max_evals=4, stop_at_score=None)

    assert _tool_result(model, 4) == {"error": native_engines.BUDGET_EXHAUSTED_MARKER}
    assert result.metadata["rounds"] == 2
    assert result.total_evals == 4
    assert len(model.calls) == 10


def test_a_task_without_cases_scores_the_whole_candidate(
    owner: tuple[User, int], engine: Engine, step_log: StepLogStore, trees: TreeStore, tmp_path: Path
) -> None:
    """With no cases, the evaluate tool scores the file once and logs it as a checkpoint.

    Args:
        owner: Alice and her budget generation.
        engine: The database.
        step_log: The step log.
        trees: The tree store.
        tmp_path: Pytest's temporary folder.
    """
    model = _Researcher()
    loop, _ = _loop(owner, engine, step_log, trees, tmp_path, model, generation=1)
    events: list[tuple[str, dict[str, Any]]] = []

    result = _run(loop, events=events, cases=[])

    assert (result.best_candidate, result.best_score, result.total_evals) == ("seedxx", 6.0, 2)
    assert _tool_result(model, 2)["score"] == 5.0
    assert [data["score"] for kind, data in events if kind == PROGRESS_CANDIDATE] == [5.0, 6.0]


def test_a_session_past_its_allowance_stops_after_the_write_up_turns(
    owner: tuple[User, int], engine: Engine, step_log: StepLogStore, trees: TreeStore, tmp_path: Path
) -> None:
    """An agent that keeps working after it spends its round's evaluations gets a fixed number of model turns.

    Args:
        owner: Alice and her budget generation.
        engine: The database.
        step_log: The step log.
        trees: The tree store.
        tmp_path: Pytest's temporary folder.
    """
    model = _Researcher(rest=_bash("true"))
    loop, _ = _loop(owner, engine, step_log, trees, tmp_path, model, generation=1)

    result = _run(loop, max_evals=2, stop_at_score=None)

    assert result.best_candidate == "seedx"
    assert len(model.calls) == 2 + ar_mod._WRITE_UP_TURNS
    assert result.metadata["rounds"] == 1


def test_a_bad_tool_call_gets_an_error_result_and_no_step(
    owner: tuple[User, int], engine: Engine, step_log: StepLogStore, trees: TreeStore, tmp_path: Path
) -> None:
    """A call to an unknown tool, or an evaluation of a path outside the lab, goes back to the model as an error.

    Args:
        owner: Alice and her budget generation.
        engine: The database.
        step_log: The step log.
        trees: The tree store.
        tmp_path: Pytest's temporary folder.
    """
    edit: Turn = lambda n: {"id": "x", "name": "edit", "arguments": "{}"}  # noqa: E731
    model = _Researcher(
        [edit, _evaluate(), lambda n: {"id": "y", "name": "evaluate", "arguments": {"file": "a/../../x"}}]
    )
    loop, boxes = _loop(owner, engine, step_log, trees, tmp_path, model, generation=1)

    with pytest.raises(RuntimeError, match="without scoring any candidate through the evaluate tool"):
        _run(loop)

    assert boxes.opened == 0
    assert [kind for _, kind, _ in _steps(engine)] == ["model"] * 4
    assert "error" in _tool_result(model, 1)
    assert "No file work/c1.txt" in _tool_result(model, 2)["error"]
    assert "inside the lab" in _tool_result(model, 3)["error"]


@pytest.mark.parametrize(
    ("arguments", "error"),
    [
        ({"file": "work/bad.txt"}, "is not UTF-8 text"),
        ({"file": "work/c1.txt", "example_ids": [["a"]]}, "list of example id strings"),
        ({"file": "work/c1.txt", "example_ids": ""}, "list of example id strings"),
        ({"file": "work/c1.txt", "example_ids": 0}, "list of example id strings"),
        ({"file": "work/c1.txt", "example_ids": False}, "list of example id strings"),
        ({"file": "work/c1.txt", "example_ids": {}}, "list of example id strings"),
        ({"file": "work/c1.txt", "example_ids": "abc"}, "list of example id strings"),
        ({"file": "work/c1.txt", "example_ids": ["zzz"]}, "Unknown or sealed example_ids"),
    ],
)
def test_a_bad_evaluate_input_gets_an_error_result_and_spends_no_budget(
    owner: tuple[User, int],
    engine: Engine,
    step_log: StepLogStore,
    trees: TreeStore,
    tmp_path: Path,
    arguments: dict[str, Any],
    error: str,
) -> None:
    """A lab file that is not UTF-8, or bad ``example_ids``, gets an error; the next sweep still scores.

    Args:
        owner: Alice and her budget generation.
        engine: The database.
        step_log: The step log.
        trees: The tree store.
        tmp_path: Pytest's temporary folder.
        arguments: The arguments of the bad evaluate call.
        error: Text that the error result holds.
    """
    bad: Turn = lambda n: {"id": "bad", "name": "evaluate", "arguments": arguments}  # noqa: E731
    model = _Researcher([_WRITE, _bash("printf '\\377' > work/bad.txt"), bad, _evaluate()])
    loop, _ = _loop(owner, engine, step_log, trees, tmp_path, model, generation=1)

    result = _run(loop, max_evals=2, stop_at_score=None)

    assert error in _tool_result(model, 3)["error"]
    assert _tool_result(model, 4)["budget"] == {"used": 2, "remaining": 0}
    assert (result.best_candidate, result.best_score, result.total_evals) == ("seedx", 5.0, 2)


@pytest.mark.parametrize(
    ("arguments", "error"),
    [
        ({"file": "work/" + "a" * 70_000}, "No file work/aaa"),
        ({"file": "work/c1.txt", "example_ids": ["z" * 70_000]}, "Unknown or sealed example_ids: ['zzz"),
    ],
    ids=["long-path", "long-ids"],
)
def test_an_evaluate_error_echoes_only_a_short_head_of_a_long_path_or_id(
    owner: tuple[User, int],
    engine: Engine,
    step_log: StepLogStore,
    trees: TreeStore,
    tmp_path: Path,
    arguments: dict[str, Any],
    error: str,
) -> None:
    """The error repeats a short head of the untrusted text, so the prompt stays under its cap and the run scores.

    Args:
        owner: Alice and her budget generation.
        engine: The database.
        step_log: The step log.
        trees: The tree store.
        tmp_path: Pytest's temporary folder.
        arguments: The arguments of the bad evaluate call, about half the prompt cap long.
        error: Text that the error result starts with.
    """
    bad: Turn = lambda n: {"id": "bad", "name": "evaluate", "arguments": arguments}  # noqa: E731
    model = _Researcher([_WRITE, bad, _evaluate()])
    loop, _ = _loop(owner, engine, step_log, trees, tmp_path, model, generation=1)

    result = _run(loop, max_evals=2, stop_at_score=None)

    message = _tool_result(model, 2)["error"]
    assert message.startswith(error)
    assert "characters in all]" in message
    assert len(message.encode("utf-8")) < 400
    assert _tool_result(model, 3)["budget"] == {"used": 2, "remaining": 0}
    assert (result.best_candidate, result.total_evals) == ("seedx", 2)


@pytest.mark.parametrize("extra", [{}, {"example_ids": None}, {"example_ids": []}], ids=["missing", "null", "empty"])
def test_no_example_ids_runs_a_full_evaluation(
    owner: tuple[User, int],
    engine: Engine,
    step_log: StepLogStore,
    trees: TreeStore,
    tmp_path: Path,
    extra: dict[str, Any],
) -> None:
    """A missing key, null or an empty list scores every visible example and logs a checkpoint.

    Args:
        owner: Alice and her budget generation.
        engine: The database.
        step_log: The step log.
        trees: The tree store.
        tmp_path: Pytest's temporary folder.
        extra: The ``example_ids`` argument, if any.
    """
    model = _Researcher([_WRITE, _evaluate(**extra)])
    loop, _ = _loop(owner, engine, step_log, trees, tmp_path, model, generation=1)
    events: list[tuple[str, dict[str, Any]]] = []

    result = _run(loop, events=events, max_evals=2, stop_at_score=None)

    answer = _tool_result(model, 2)
    assert (answer["num_evaluated"], answer["budget"]) == (2, {"used": 2, "remaining": 0})
    assert [(data["candidate_id"], data["score"]) for kind, data in events if kind == PROGRESS_CANDIDATE] == [
        ("0", 5.0)
    ]
    assert (result.best_candidate, result.best_score, result.total_evals) == ("seedx", 5.0, 2)


def test_a_lab_file_whose_name_starts_with_two_dots_is_scored(
    owner: tuple[User, int], engine: Engine, step_log: StepLogStore, trees: TreeStore, tmp_path: Path
) -> None:
    """Only a path that leaves the lab is refused; ``..notes.txt`` is a file inside it.

    Args:
        owner: Alice and her budget generation.
        engine: The database.
        step_log: The step log.
        trees: The tree store.
        tmp_path: Pytest's temporary folder.
    """
    notes: Turn = lambda n: {"id": "z", "name": "evaluate", "arguments": {"file": "..notes.txt"}}  # noqa: E731
    model = _Researcher([_bash("printf %s seedx > ..notes.txt"), notes])
    loop, _ = _loop(owner, engine, step_log, trees, tmp_path, model, generation=1)

    result = _run(loop, max_evals=2)

    assert _tool_result(model, 2)["average_score"] == 5.0
    assert (result.best_candidate, result.best_score) == ("seedx", 5.0)


def test_a_session_that_only_runs_commands_stops_at_the_tool_call_cap(
    owner: tuple[User, int], engine: Engine, step_log: StepLogStore, trees: TreeStore, tmp_path: Path
) -> None:
    """With no cost cap, an agent that never evaluates stops at its third tool call, before a fourth model call.

    Args:
        owner: Alice and her budget generation.
        engine: The database.
        step_log: The step log.
        trees: The tree store.
        tmp_path: Pytest's temporary folder.
    """
    model = _Researcher([], rest=_bash("true"))
    loop, boxes = _loop(owner, engine, step_log, trees, tmp_path, model, generation=1)

    with pytest.raises(RuntimeError, match="without scoring any candidate through the evaluate tool"):
        _run(loop, native_options=_options(max_tool_calls=3))

    assert len(model.calls) == 3
    assert boxes.opened == 3
    assert [kind for _, kind, _ in _steps(engine)] == ["model", "command"] * 3


@pytest.mark.parametrize("seconds", [60, 45])
def test_the_no_evaluation_watchdog_counts_model_turns_since_the_budget_last_changed(
    seconds: int, owner: tuple[User, int], engine: Engine, step_log: StepLogStore, trees: TreeStore, tmp_path: Path
) -> None:
    """Sixty seconds is two turns, and 45 rounds up to two: each round gets two turns after its sweep, then stops.

    Args:
        seconds: The ``max_no_eval_seconds`` option.
        owner: Alice and her budget generation.
        engine: The database.
        step_log: The step log.
        trees: The tree store.
        tmp_path: Pytest's temporary folder.
    """
    model = _Researcher(rest=_bash("true"))
    loop, _ = _loop(owner, engine, step_log, trees, tmp_path, model, generation=1)

    result = _run(loop, native_options=_options(max_no_eval_seconds=seconds))

    assert (result.best_candidate, result.best_score) == ("seedxx", 6.0)
    assert result.metadata["rounds"] == 2
    assert len(model.calls) == 8
    round_steps = ["model", "command", "model", "model", "command", "model", "command"]
    assert [kind for _, kind, _ in _steps(engine)] == round_steps * 2


def test_the_proposer_cost_cap_stops_the_session(
    owner: tuple[User, int], engine: Engine, step_log: StepLogStore, trees: TreeStore, tmp_path: Path
) -> None:
    """With a cap of two cents, the round runs its two replies' tools and then asks the model no more.

    Args:
        owner: Alice and her budget generation.
        engine: The database.
        step_log: The step log.
        trees: The tree store.
        tmp_path: Pytest's temporary folder.
    """
    model = _Researcher(cost="1")
    loop, boxes = _loop(owner, engine, step_log, trees, tmp_path, model, generation=1)
    result = _run(loop, native_options=_options(max_token_cost=0.02))

    assert result.best_candidate == "seedx"
    assert boxes.opened == 1
    assert [kind for _, kind, _ in _steps(engine)] == ["model", "command", "model"]
    assert result.metadata["rounds"] == 1


@pytest.mark.parametrize("cap", [None, 0.03])
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
    first, _ = _loop(owner, engine, step_log, trees, tmp_path, _Researcher(billed_at=frozenset({0})), generation=1)
    live = _run(first, native_options=options)

    _new_generation(engine)
    model = _Researcher()
    loop, _ = _loop(owner, engine, step_log, trees, tmp_path, model, generation=2)
    replayed = _run(loop, native_options=options)

    assert model.calls == []
    if cap is None:
        # Six replies of one cent each and the billed failure of one cent.
        assert live.metadata["proposer_cost_usd"] == replayed.metadata["proposer_cost_usd"] == 0.07
    else:
        # The billed failure and the first two replies reach the cap, so the round runs their tools and then asks
        # the model no more.
        assert [kind for _, kind, _ in _steps(engine)] == ["model", "model", "command", "model"]
        assert live.metadata["proposer_cost_usd"] == replayed.metadata["proposer_cost_usd"] == 0.03
        assert live.metadata["rounds"] == replayed.metadata["rounds"] == 1


@pytest.mark.parametrize("every_case", [False, True])
def test_a_repository_run_scores_each_tree_and_keeps_a_metric_error_out_of_the_lead(
    every_case: bool,
    owner: tuple[User, int],
    engine: Engine,
    step_log: StepLogStore,
    trees: TreeStore,
    tmp_path: Path,
) -> None:
    """Each round edits the leader's tree; the engine sweeps the tree it leaves, without the lab, as one version.

    Round one's version is a metric error, on one case or on every case. It shows as a candidate with no score, and
    round two starts again from the seed. Round two's version reaches the target and is the final pick (SKY-825).

    Args:
        every_case: Give the metric error on every case, so the version has no finite mean.
        owner: Alice and her budget generation.
        engine: The database.
        step_log: The step log.
        trees: The tree store.
        tmp_path: Pytest's temporary folder.
    """
    scored: list[tuple[str, str, float | None]] = []

    def score(loop: EngineLoop, candidate: Any, case: Any) -> tuple[float | None, dict[str, Any]]:
        """Score a version tree by the length of its ``a.py``, with a metric error at a length of two.

        Args:
            loop: The engine loop.
            candidate: The version's tree id.
            case: The case.

        Returns:
            The length, or ``None`` for the metric error, and no side information.
        """
        assert loop.list_files(candidate, ".autoresearch") is None
        text = (loop.read_file(candidate, "a.py") or b"").decode()
        value = None if len(text) == 2 and (every_case or case == _CASES[1]) else float(len(text))
        scored.append((candidate, text, value))
        return value, {}

    model = _Researcher([_bash("printf %s {x} >> a.py && printf '## Round {n}\\n' >> .autoresearch/notebook.md")])
    loop, _ = _loop(owner, engine, step_log, trees, tmp_path, model, generation=1)
    seed = _tree(trees, "a.py", "a")
    events: list[tuple[str, dict[str, Any]]] = []

    result = _run(loop, events=events, stop_at_score=3.0, score=score, repo_seed=RepoSeed(tree_id=seed))

    # Round two appends to the seed, not to the metric-error version.
    assert [(text, value) for _, text, value in scored] == [
        ("a", 1.0),
        ("a", 1.0),
        ("ax", None if every_case else 2.0),
        ("ax", None),
        ("axx", 3.0),
        ("axx", 3.0),
    ]
    versions = list(dict.fromkeys(candidate for candidate, _, _ in scored))
    assert versions[0] == seed
    assert len(versions) == 3
    assert (result.best_candidate, result.best_score) == (versions[2], 3.0)
    assert loop.read_file(result.best_candidate, "a.py") == b"axx"
    assert [data["score"] for kind, data in events if kind == PROGRESS_CANDIDATE] == [1.0, None, 3.0]
    # The agent's notebook comes back from each round's lab.
    notebook = next(tmp_path.rglob("lab/notebook.md")).read_text()
    assert "## Round 1" in notebook
    assert "## Round 2" in notebook
    assert model.tools[0] == [ar_mod.REPO_BASH_TOOL]
    # The seed's score is the baseline of round one, so its failed version is no raise of the leader.
    assert result.metadata["directives"] == ["survey", "explore"]


def test_a_repository_round_starts_from_the_best_frontier_version(
    owner: tuple[User, int], engine: Engine, step_log: StepLogStore, trees: TreeStore, tmp_path: Path
) -> None:
    """Round one's version beats the seed, so round two edits round one's tree (SKY-825).

    Args:
        owner: Alice and her budget generation.
        engine: The database.
        step_log: The step log.
        trees: The tree store.
        tmp_path: Pytest's temporary folder.
    """
    scored: list[bytes | None] = []

    def score(loop: EngineLoop, candidate: Any, case: Any) -> tuple[float | None, dict[str, Any]]:
        """Record the scored ``a.py``, and score it by its length.

        Args:
            loop: The engine loop.
            candidate: The version's tree id.
            case: The case.

        Returns:
            The length and no side information.
        """
        scored.append(loop.read_file(candidate, "a.py"))
        return _score_a_py(loop, candidate, case)

    model = _Researcher([_bash("printf %s {x} >> a.py")])
    loop, _ = _loop(owner, engine, step_log, trees, tmp_path, model, generation=1)

    result = _run(loop, stop_at_score=4.0, score=score, repo_seed=RepoSeed(tree_id=_tree(trees, "a.py", "a")))

    assert scored == [b"a", b"a", b"ax", b"ax", b"axxx", b"axxx"]
    assert loop.read_file(result.best_candidate, "a.py") == b"axxx"
    assert result.metadata["directives"] == ["survey", "exploit"]


def test_a_repository_metric_error_with_no_cases_shows_as_a_candidate_with_no_score(
    owner: tuple[User, int], engine: Engine, step_log: StepLogStore, trees: TreeStore, tmp_path: Path
) -> None:
    """With no cases, round one's version is a metric error: it still shows, with no score (SKY-825).

    Args:
        owner: Alice and her budget generation.
        engine: The database.
        step_log: The step log.
        trees: The tree store.
        tmp_path: Pytest's temporary folder.
    """

    def score(loop: EngineLoop, candidate: Any, case: Any) -> tuple[float | None, dict[str, Any]]:
        """Give an ``a.py`` of length two a metric error, and score any other by its length.

        Args:
            loop: The engine loop.
            candidate: The version's tree id.
            case: The case.

        Returns:
            The score, or ``None`` for the metric error, and no side information.
        """
        value, info = _score_a_py(loop, candidate, case)
        return (None if value == 2.0 else value), info

    model = _Researcher([_bash("printf %s {x} >> a.py")])
    loop, _ = _loop(owner, engine, step_log, trees, tmp_path, model, generation=1)
    events: list[tuple[str, dict[str, Any]]] = []

    result = _run(
        loop,
        events=events,
        cases=[],
        stop_at_score=3.0,
        score=score,
        repo_seed=RepoSeed(tree_id=_tree(trees, "a.py", "a")),
    )

    assert [data["score"] for kind, data in events if kind == PROGRESS_CANDIDATE] == [1.0, None, 3.0]
    assert loop.read_file(result.best_candidate, "a.py") == b"axx"


def test_a_repository_round_that_leaves_a_scored_version_spends_no_budget_and_ends_the_run(
    owner: tuple[User, int], engine: Engine, step_log: StepLogStore, trees: TreeStore, tmp_path: Path
) -> None:
    """A round that changes only the notebook leaves the seed's tree, which has its score: the run stops.

    Args:
        owner: Alice and her budget generation.
        engine: The database.
        step_log: The step log.
        trees: The tree store.
        tmp_path: Pytest's temporary folder.
    """
    model = _Researcher([_bash("printf n >> .autoresearch/notebook.md")])
    loop, _ = _loop(owner, engine, step_log, trees, tmp_path, model, generation=1)
    seed = _tree(trees, "a.py", "a")

    result = _run(loop, score=_score_a_py, repo_seed=RepoSeed(tree_id=seed))

    assert (result.best_candidate, result.total_evals, result.metadata["rounds"]) == (seed, 2, 1)


def test_a_repository_session_has_no_no_evaluation_watchdog(
    owner: tuple[User, int], engine: Engine, step_log: StepLogStore, trees: TreeStore, tmp_path: Path
) -> None:
    """A repository session cannot evaluate, so turns with no budget change do not stop it.

    Args:
        owner: Alice and her budget generation.
        engine: The database.
        step_log: The step log.
        trees: The tree store.
        tmp_path: Pytest's temporary folder.
    """
    model = _Researcher([_bash("printf x >> a.py"), _bash("printf y >> a.py")])
    loop, _ = _loop(owner, engine, step_log, trees, tmp_path, model, generation=1)

    result = _run(
        loop,
        stop_at_score=3.0,
        score=_score_a_py,
        repo_seed=RepoSeed(tree_id=_tree(trees, "a.py", "a")),
        native_options=_options(max_no_eval_seconds=30),
    )

    assert loop.read_file(result.best_candidate, "a.py") == b"axy"


def test_a_repository_budget_that_cannot_score_a_version_stops_before_the_session(
    owner: tuple[User, int], engine: Engine, step_log: StepLogStore, trees: TreeStore, tmp_path: Path
) -> None:
    """After the seed's sweep, one evaluation is left for two cases, so no session starts.

    Args:
        owner: Alice and her budget generation.
        engine: The database.
        step_log: The step log.
        trees: The tree store.
        tmp_path: Pytest's temporary folder.
    """
    model = _Researcher([_bash("printf x >> a.py")])
    loop, _ = _loop(owner, engine, step_log, trees, tmp_path, model, generation=1)
    seed = _tree(trees, "a.py", "a")

    result = _run(loop, max_evals=3, score=_score_a_py, repo_seed=RepoSeed(tree_id=seed))

    assert model.calls == []
    assert (result.best_candidate, result.total_evals) == (seed, 2)


def test_a_repository_budget_that_cannot_score_the_seed_fails_the_run_before_any_session(
    owner: tuple[User, int], engine: Engine, step_log: StepLogStore, trees: TreeStore, tmp_path: Path
) -> None:
    """One evaluation is left for two cases, so the seed gets no sweep: the run ends with no session (SKY-825).

    A repository session has no ``evaluate`` tool, so it could never score a version.

    Args:
        owner: Alice and her budget generation.
        engine: The database.
        step_log: The step log.
        trees: The tree store.
        tmp_path: Pytest's temporary folder.
    """
    model = _Researcher([_bash("printf x >> a.py")])
    loop, _ = _loop(owner, engine, step_log, trees, tmp_path, model, generation=1)

    with pytest.raises(RuntimeError, match="needs 2 evaluation units to score the seed") as raised:
        _run(loop, max_evals=1, score=_score_a_py, repo_seed=RepoSeed(tree_id=_tree(trees, "a.py", "a")))
    # A BudgetExhaustedError would end a whole Auto run, not only this engine's lane.
    assert type(raised.value) is RuntimeError
    assert model.calls == []


def test_a_repository_budget_that_scores_only_the_seed_ends_the_run_on_the_seed(
    owner: tuple[User, int], engine: Engine, step_log: StepLogStore, trees: TreeStore, tmp_path: Path
) -> None:
    """Two evaluations are left for two cases, so the seed gets its sweep and no session starts (SKY-825).

    Args:
        owner: Alice and her budget generation.
        engine: The database.
        step_log: The step log.
        trees: The tree store.
        tmp_path: Pytest's temporary folder.
    """
    model = _Researcher([_bash("printf x >> a.py")])
    loop, _ = _loop(owner, engine, step_log, trees, tmp_path, model, generation=1)
    seed = _tree(trees, "a.py", "a")

    result = _run(loop, max_evals=2, score=_score_a_py, repo_seed=RepoSeed(tree_id=seed))

    assert model.calls == []
    assert (result.best_candidate, result.total_evals) == (seed, 2)


def test_a_text_budget_too_small_for_a_full_sweep_still_starts_the_session(
    owner: tuple[User, int], engine: Engine, step_log: StepLogStore, trees: TreeStore, tmp_path: Path
) -> None:
    """A text session has the ``evaluate`` tool, so one evaluation for two cases is not out of budget (SKY-825).

    The session spends the unit on a one-case probe, which is no full sweep, so the run has no scored candidate.

    Args:
        owner: Alice and her budget generation.
        engine: The database.
        step_log: The step log.
        trees: The tree store.
        tmp_path: Pytest's temporary folder.
    """
    model = _Researcher([_WRITE, _evaluate(example_ids=["0"])])
    loop, _ = _loop(owner, engine, step_log, trees, tmp_path, model, generation=1)

    with pytest.raises(RuntimeError, match="finished without scoring any candidate"):
        _run(loop, max_evals=1, stop_at_score=None)

    assert model.calls
    assert _tool_result(model, 2)["average_score"] == 5.0


def test_a_repository_session_reads_the_task_brief_and_state_in_the_lab(
    owner: tuple[User, int], engine: Engine, step_log: StepLogStore, trees: TreeStore, tmp_path: Path
) -> None:
    """The engine writes ``TASK.md``, ``BRIEF.md`` and ``STATE.md`` over the parent's tree, under ``.autoresearch/``.

    Args:
        owner: Alice and her budget generation.
        engine: The database.
        step_log: The step log.
        trees: The tree store.
        tmp_path: Pytest's temporary folder.
    """
    lab = ar_mod._REPO_LAB
    model = _Researcher([_bash(f"test -s {lab}/TASK.md && test -s {lab}/BRIEF.md && cat {lab}/STATE.md")])
    loop, _ = _loop(owner, engine, step_log, trees, tmp_path, model, generation=1)

    _run(loop, score=_score_a_py, repo_seed=RepoSeed(tree_id=_tree(trees, "a.py", "a")))

    output = _tool_result(model, 1)
    assert output["exit_code"] == 0
    assert "The engine gives this round's version one full evaluation" in output["stdout"]


def test_a_notebook_that_a_repository_session_deletes_does_not_come_back(
    owner: tuple[User, int], engine: Engine, step_log: StepLogStore, trees: TreeStore, tmp_path: Path
) -> None:
    """Round one writes the notebook and round two deletes it, so the local lab has no notebook after the run.

    Args:
        owner: Alice and her budget generation.
        engine: The database.
        step_log: The step log.
        trees: The tree store.
        tmp_path: Pytest's temporary folder.
    """
    command = (
        "printf %s {x} >> a.py && if [ {n} = 1 ]; then printf n > .autoresearch/notebook.md; "
        "else rm .autoresearch/notebook.md; fi"
    )
    model = _Researcher([_bash(command)])
    loop, _ = _loop(owner, engine, step_log, trees, tmp_path, model, generation=1)

    result = _run(loop, stop_at_score=4.0, score=_score_a_py, repo_seed=RepoSeed(tree_id=_tree(trees, "a.py", "a")))

    assert result.metadata["rounds"] == 2
    assert list(tmp_path.rglob("lab/notebook.md")) == []


_ENTRY = (
    '[ "$APP_PROMPT_FILE" = "$ASCENT_TASK" ] && [ "$APP_MODEL" = m ] '
    '&& grep -q "^## Directive" "$ASCENT_TASK" && grep -q "^# Research notebook" "$ASCENT_TASK" '
    '&& [ "$(cat deps/lib.txt)" = lib ] && printf x >> a.py && printf n >> .autoresearch/notebook.md'
)


def _command_options(entry: str = _ENTRY, setup: list[str] | None = None) -> SimpleNamespace:
    """Build native options that run a fake ``[proposer] entry`` command in place of the tool loop.

    Args:
        entry: The shell command.
        setup: The ``[proposer] setup`` argv; one that writes ``deps/lib.txt`` when ``None``.

    Returns:
        The options.
    """
    options = _options(harness=BLACKBOX_HARNESS_CUSTOM, run_command=entry)
    options.gateway = SimpleNamespace(url="")
    options.repo = {
        "file_proposer": True,
        "proposer_setup": ["sh", "-c", "mkdir deps && printf lib > deps/lib.txt"] if setup is None else setup,
        "proposer_hosts": [],
    }
    return options


def test_a_files_proposer_command_makes_each_version_and_the_model_is_never_called(
    owner: tuple[User, int], engine: Engine, step_log: StepLogStore, trees: TreeStore, tmp_path: Path
) -> None:
    """Each round runs the entry once, with the setup's output, and its tree without the lab is the scored version.

    Args:
        owner: Alice and her budget generation.
        engine: The database.
        step_log: The step log.
        trees: The tree store.
        tmp_path: Pytest's temporary folder.
    """
    model = _Researcher()
    loop, _ = _loop(owner, engine, step_log, trees, tmp_path, model, generation=1)
    seed = _tree(trees, "a.py", "a")
    scored: list[str] = []

    def score(loop: EngineLoop, candidate: Any, case: Any) -> tuple[float | None, dict[str, Any]]:
        """Record the candidate and score it by the length of its ``a.py``.

        Args:
            loop: The engine loop.
            candidate: The version's tree id.
            case: The case.

        Returns:
            The score and no side information.
        """
        scored.append(candidate)
        return _score_a_py(loop, candidate, case)

    result = _run(
        loop, stop_at_score=4.0, score=score, repo_seed=RepoSeed(tree_id=seed), native_options=_command_options()
    )

    assert model.calls == []
    versions = list(dict.fromkeys(scored))
    assert [loop.read_file(version, "a.py") for version in versions] == [b"a", b"ax", b"axx", b"axxx"]
    for version in versions:
        assert loop.list_files(version, ".") == ["a.py"]
    assert result.best_candidate == versions[-1]
    assert result.metadata["rounds"] == 3


def test_a_failed_files_proposer_command_leaves_no_version_and_does_not_crash_the_run(
    owner: tuple[User, int],
    engine: Engine,
    step_log: StepLogStore,
    trees: TreeStore,
    tmp_path: Path,
    caplog: pytest.LogCaptureFixture,
) -> None:
    """A command that exits 1 leaves the seed best and ends the run, and one engine warning says why.

    Args:
        owner: Alice and her budget generation.
        engine: The database.
        step_log: The step log.
        trees: The tree store.
        tmp_path: Pytest's temporary folder.
        caplog: Pytest's log capture.
    """
    loop, _ = _loop(owner, engine, step_log, trees, tmp_path, _Researcher(), generation=1)
    seed = _tree(trees, "a.py", "a")

    with caplog.at_level(logging.WARNING, logger="app.engine"):
        result = _run(
            loop,
            score=_score_a_py,
            repo_seed=RepoSeed(tree_id=seed),
            native_options=_command_options("printf x >> a.py; exit 1"),
        )

    assert result.best_candidate == seed
    warnings = [record.getMessage() for record in caplog.records if record.name == "app.engine"]
    assert warnings == ["Proposer session round1 failed: The [proposer] entry command failed with exit code 1."]


def test_a_files_proposer_setup_that_changes_a_seed_file_fails_the_run(
    owner: tuple[User, int], engine: Engine, step_log: StepLogStore, trees: TreeStore, tmp_path: Path
) -> None:
    """A setup output would enter every version when it changes a seed file, so the run stops before any command.

    Args:
        owner: Alice and her budget generation.
        engine: The database.
        step_log: The step log.
        trees: The tree store.
        tmp_path: Pytest's temporary folder.
    """
    loop, _ = _loop(owner, engine, step_log, trees, tmp_path, _Researcher(), generation=1)

    with pytest.raises(ServiceError, match=r"changed or removed files of the repository: a\.py\. "):
        _run(
            loop,
            score=_score_a_py,
            repo_seed=RepoSeed(tree_id=_tree(trees, "a.py", "a")),
            native_options=_command_options(setup=["sh", "-c", "printf b >> a.py"]),
        )


def test_a_files_proposer_setup_name_with_a_new_line_stays_out_of_every_version(
    owner: tuple[User, int], engine: Engine, step_log: StepLogStore, trees: TreeStore, tmp_path: Path
) -> None:
    """The setup listing is NUL-separated, so a folder name with a new line is one path, and no version holds it.

    Args:
        owner: Alice and her budget generation.
        engine: The database.
        step_log: The step log.
        trees: The tree store.
        tmp_path: Pytest's temporary folder.
    """
    loop, _ = _loop(owner, engine, step_log, trees, tmp_path, _Researcher(), generation=1)
    setup = ["sh", "-c", 'mkdir deps "$(printf "x\\ny")" && printf lib > deps/lib.txt && touch "$(printf "x\\ny")/f"']
    scored: list[str] = []

    def score(loop: EngineLoop, candidate: Any, case: Any) -> tuple[float | None, dict[str, Any]]:
        """Record the candidate and score it by the length of its ``a.py``.

        Args:
            loop: The engine loop.
            candidate: The version's tree id.
            case: The case.

        Returns:
            The score and no side information.
        """
        scored.append(candidate)
        return _score_a_py(loop, candidate, case)

    _run(
        loop,
        stop_at_score=2.0,
        score=score,
        repo_seed=RepoSeed(tree_id=_tree(trees, "a.py", "a")),
        native_options=_command_options(setup=setup),
    )

    versions = list(dict.fromkeys(scored))
    assert len(versions) == 2
    for version in versions:
        assert loop.list_files(version, ".") == ["a.py"]


def test_a_files_proposer_setup_lists_only_the_top_folder_of_its_output(
    owner: tuple[User, int], engine: Engine, step_log: StepLogStore, trees: TreeStore, tmp_path: Path
) -> None:
    """The setup box keeps only a new path whose folder existed before, so a nested output is one root to remove.

    Args:
        owner: Alice and her budget generation.
        engine: The database.
        step_log: The step log.
        trees: The tree store.
        tmp_path: Pytest's temporary folder.
    """
    loop, _ = _loop(owner, engine, step_log, trees, tmp_path, _Researcher(), generation=1)
    seed = _tree(trees, "a.py", "a")
    ctx = loop.context(repo_seed=RepoSeed(tree_id=seed))
    run = ctx.run_command
    assert run is not None
    commands: list[str] = []

    def record(tree: str, command: str, *args: Any, **kwargs: Any) -> Any:
        """Record the command, then run it in its box.

        Args:
            tree: The tree the command starts from.
            command: The shell command.
            *args: The grants.
            **kwargs: The time limit.

        Returns:
            The command's result and tree.
        """
        commands.append(command)
        return run(tree, command, *args, **kwargs)

    ctx = dataclasses.replace(ctx, run_command=record)
    setup = ["sh", "-c", "mkdir -p deps/sub && printf lib > deps/sub/lib.txt && printf x > deps/top.txt"]
    proposer = loop_tools.FileProposer(ctx, _command_options(setup=setup), ".autoresearch")  # type: ignore[arg-type]

    base = proposer.base(ctx.repo_seed.tree_id)  # type: ignore[union-attr]
    proposer.version("start", base, base)

    assert proposer._roots == ["deps"]
    assert commands[-1] == "rm -rf -- .autoresearch deps"


def test_a_files_proposer_setup_folder_inside_a_seed_folder_stays_out_of_every_version(
    owner: tuple[User, int], engine: Engine, step_log: StepLogStore, trees: TreeStore, tmp_path: Path
) -> None:
    """A new folder below a seed folder is a setup output, so the setup does not count as a seed change.

    Args:
        owner: Alice and her budget generation.
        engine: The database.
        step_log: The step log.
        trees: The tree store.
        tmp_path: Pytest's temporary folder.
    """
    loop, _ = _loop(owner, engine, step_log, trees, tmp_path, _Researcher(), generation=1)
    scored: list[str] = []

    def score(loop: EngineLoop, candidate: Any, case: Any) -> tuple[float | None, dict[str, Any]]:
        """Record the candidate and score it by the length of its ``a.py``.

        Args:
            loop: The engine loop.
            candidate: The version's tree id.
            case: The case.

        Returns:
            The score and no side information.
        """
        scored.append(candidate)
        return _score_a_py(loop, candidate, case)

    _run(
        loop,
        stop_at_score=2.0,
        score=score,
        repo_seed=RepoSeed(tree_id=_store(trees, {"a.py": b"a", "src/a.py": b"s"})),
        native_options=_command_options("printf x >> a.py", setup=["sh", "-c", "mkdir src/cache && touch src/cache/c"]),
    )

    versions = list(dict.fromkeys(scored))
    assert len(versions) == 2
    for version in versions:
        assert loop.list_files(version, "src") == ["src/a.py"]


def test_a_files_proposer_notebook_line_reaches_the_next_round(
    owner: tuple[User, int], engine: Engine, step_log: StepLogStore, trees: TreeStore, tmp_path: Path
) -> None:
    """Round one's command writes a notebook line, and round two's task file and lab notebook hold it.

    Args:
        owner: Alice and her budget generation.
        engine: The database.
        step_log: The step log.
        trees: The tree store.
        tmp_path: Pytest's temporary folder.
    """
    loop, _ = _loop(owner, engine, step_log, trees, tmp_path, _Researcher(), generation=1)
    entry = (
        'if grep -q carried "$ASCENT_TASK" && grep -q carried .autoresearch/notebook.md; then printf y >> a.py; '
        "else printf x >> a.py && printf 'carried\\n' >> .autoresearch/notebook.md; fi"
    )
    scored: list[str] = []

    def score(loop: EngineLoop, candidate: Any, case: Any) -> tuple[float | None, dict[str, Any]]:
        """Record the candidate and score it by the length of its ``a.py``.

        Args:
            loop: The engine loop.
            candidate: The version's tree id.
            case: The case.

        Returns:
            The score and no side information.
        """
        scored.append(candidate)
        return _score_a_py(loop, candidate, case)

    _run(
        loop,
        stop_at_score=3.0,
        score=score,
        repo_seed=RepoSeed(tree_id=_tree(trees, "a.py", "a")),
        native_options=_command_options(entry, setup=[]),
    )

    versions = list(dict.fromkeys(scored))
    assert [loop.read_file(version, "a.py") for version in versions] == [b"a", b"ax", b"axy"]


def test_a_files_proposer_setup_that_fails_stops_the_run_with_its_command_and_exit_code(
    owner: tuple[User, int], engine: Engine, step_log: StepLogStore, trees: TreeStore, tmp_path: Path
) -> None:
    """A setup that exits 3 stops the run before any entry command, and the error names the setup and the code.

    Args:
        owner: Alice and her budget generation.
        engine: The database.
        step_log: The step log.
        trees: The tree store.
        tmp_path: Pytest's temporary folder.
    """
    loop, _ = _loop(owner, engine, step_log, trees, tmp_path, _Researcher(), generation=1)

    with pytest.raises(ServiceError, match=r"\[proposer\] setup command .*sh -c 'exit 3'.* failed with exit code 3"):
        _run(
            loop,
            score=_score_a_py,
            repo_seed=RepoSeed(tree_id=_tree(trees, "a.py", "a")),
            native_options=_command_options(setup=["sh", "-c", "exit 3"]),
        )


def test_a_files_proposer_version_past_the_evaluation_budget_ends_the_run_without_an_error(
    owner: tuple[User, int], engine: Engine, step_log: StepLogStore, trees: TreeStore, tmp_path: Path
) -> None:
    """The seed sweep takes two of three scorer calls, so round one's version cannot get its full sweep.

    Args:
        owner: Alice and her budget generation.
        engine: The database.
        step_log: The step log.
        trees: The tree store.
        tmp_path: Pytest's temporary folder.
    """
    loop, _ = _loop(owner, engine, step_log, trees, tmp_path, _Researcher(), generation=1)
    seed = _tree(trees, "a.py", "a")

    result = _run(
        loop,
        max_evals=3,
        stop_at_score=None,
        score=_score_a_py,
        repo_seed=RepoSeed(tree_id=seed),
        native_options=_command_options(),
    )

    # The sweep refuses the version whole, so the third scorer call stays unused.
    assert result.best_candidate == seed
    assert result.metadata["rounds"] == 1
    assert result.total_evals == 2


def test_a_files_proposer_run_replays_on_a_resume_with_no_box(
    owner: tuple[User, int], engine: Engine, step_log: StepLogStore, trees: TreeStore, tmp_path: Path
) -> None:
    """The setup and each entry command are committed steps, so a new generation replays the run with no box.

    Args:
        owner: Alice and her budget generation.
        engine: The database.
        step_log: The step log.
        trees: The tree store.
        tmp_path: Pytest's temporary folder.
    """
    seed = _tree(trees, "a.py", "a")
    first, first_boxes = _loop(owner, engine, step_log, trees, tmp_path, _Researcher(), generation=1)
    live = _run(
        first, stop_at_score=4.0, score=_score_a_py, repo_seed=RepoSeed(tree_id=seed), native_options=_command_options()
    )
    assert first_boxes.opened > 0

    _new_generation(engine)
    loop, boxes = _loop(owner, engine, step_log, trees, tmp_path, _Researcher(), generation=2)
    replayed = _run(
        loop, stop_at_score=4.0, score=_score_a_py, repo_seed=RepoSeed(tree_id=seed), native_options=_command_options()
    )
    loop.finish()

    assert (replayed.best_candidate, replayed.best_score) == (live.best_candidate, live.best_score)
    assert loop.read_file(replayed.best_candidate, "a.py") == b"axxx"
    assert boxes.opened == 0


def test_a_files_proposer_in_a_protected_run_gets_the_scoped_model_key(
    owner: tuple[User, int], engine: Engine, step_log: StepLogStore, trees: TreeStore, tmp_path: Path
) -> None:
    """The setup and the entry boxes get the run's route key and model, so the command can reach the model relay.

    Args:
        owner: Alice and her budget generation.
        engine: The database.
        step_log: The step log.
        trees: The tree store.
        tmp_path: Pytest's temporary folder.
    """
    loop, _ = _loop(owner, engine, step_log, trees, tmp_path, _Researcher(), generation=1)
    seed = _tree(trees, "a.py", "a")
    relay = '[ "$APP_API_KEY" = route-key ] && [ "$APP_MODEL" = m ]'
    options = _command_options(
        entry=f"{relay} && printf x >> a.py", setup=["sh", "-c", f"{relay} && mkdir deps && printf lib > deps/lib.txt"]
    )
    options.budget_route = {"url": "https://gw.example/v1", "token": "route-key"}
    options.gateway = SimpleNamespace(url="https://gw.example/v1", api_key="route-key")

    result = _run(loop, stop_at_score=2.0, score=_score_a_py, repo_seed=RepoSeed(tree_id=seed), native_options=options)

    assert loop.read_file(result.best_candidate, "a.py") == b"ax"


def _protected_options(entry: str, setup: list[str]) -> SimpleNamespace:
    """Build the native options of a protected run whose route key is ``_ROUTE_KEY``.

    Args:
        entry: The ``[proposer] entry`` command.
        setup: The ``[proposer] setup`` argv.

    Returns:
        The options.
    """
    options = _command_options(entry=entry, setup=setup)
    options.budget_route = {"url": "https://gw.example/v1", "token": _ROUTE_KEY}
    options.gateway = SimpleNamespace(url="https://gw.example/v1", api_key=_ROUTE_KEY)
    return options


# Long enough that no other text of the run holds it by chance.
_ROUTE_KEY = "scoped-route-key-of-this-test"


def test_an_entry_that_prints_the_route_key_leaves_it_out_of_the_step_log_and_the_run_events(
    owner: tuple[User, int],
    engine: Engine,
    step_log: StepLogStore,
    trees: TreeStore,
    tmp_path: Path,
    caplog: pytest.LogCaptureFixture,
) -> None:
    """The step log and the failure's run event keep the command's output, so the key shows as ``[redacted]``.

    Args:
        owner: Alice and her budget generation.
        engine: The database.
        step_log: The step log.
        trees: The tree store.
        tmp_path: Pytest's temporary folder.
        caplog: Pytest's log capture.
    """
    loop, _ = _loop(owner, engine, step_log, trees, tmp_path, _Researcher(), generation=1)
    seed = _tree(trees, "a.py", "a")
    events: list[tuple[str, dict[str, Any]]] = []
    entry = 'echo "$APP_API_KEY"; echo "key $APP_API_KEY" >&2; exit 1'
    options = _protected_options(entry, ["sh", "-c", "mkdir deps"])

    with caplog.at_level(logging.WARNING):
        _run(loop, events=events, score=_score_a_py, repo_seed=RepoSeed(tree_id=seed), native_options=options)

    bodies = [
        json.dumps(step_log.get_step(step_id_for(RUN_ID, ROOT_AGENT_ID, seq)))
        for seq, kind, _ in _steps(engine)
        if kind == "command"
    ]
    assert any("key [redacted]" in body for body in bodies)
    assert not any(_ROUTE_KEY in body for body in bodies)
    assert "key [redacted]" in caplog.text
    assert _ROUTE_KEY not in caplog.text
    assert _ROUTE_KEY not in json.dumps(events, default=str)


def test_a_setup_that_prints_the_route_key_fails_with_it_redacted(
    owner: tuple[User, int], engine: Engine, step_log: StepLogStore, trees: TreeStore, tmp_path: Path
) -> None:
    """The setup's error copies the end of its output, so the key shows as ``[redacted]`` there.

    Args:
        owner: Alice and her budget generation.
        engine: The database.
        step_log: The step log.
        trees: The tree store.
        tmp_path: Pytest's temporary folder.
    """
    loop, _ = _loop(owner, engine, step_log, trees, tmp_path, _Researcher(), generation=1)
    seed = _tree(trees, "a.py", "a")
    options = _protected_options("true", ["sh", "-c", 'echo "key $APP_API_KEY" >&2; exit 3'])

    with pytest.raises(ServiceError) as error:
        _run(loop, score=_score_a_py, repo_seed=RepoSeed(tree_id=seed), native_options=options)

    assert "key [redacted]" in str(error.value)
    assert _ROUTE_KEY not in str(error.value)


@pytest.mark.parametrize("links", [{}, {".autoresearch": "a.py"}, {".autoresearch": "missing"}])
def test_a_repository_with_an_entry_at_the_lab_path_is_refused(
    links: dict[str, str],
    owner: tuple[User, int],
    engine: Engine,
    step_log: StepLogStore,
    trees: TreeStore,
    tmp_path: Path,
) -> None:
    """The engine removes ``.autoresearch`` from each version, so a repository file or symlink there would be lost.

    Args:
        links: A symlink at the lab path, to a file or to a missing path; a file there when empty.
        owner: Alice and her budget generation.
        engine: The database.
        step_log: The step log.
        trees: The tree store.
        tmp_path: Pytest's temporary folder.
    """
    model = _Researcher()
    loop, _ = _loop(owner, engine, step_log, trees, tmp_path, model, generation=1)

    seed = _store(trees, {"a.py": b"a"} if links else {".autoresearch": b"a"}, links)

    with pytest.raises(ServiceError, match="lab"):
        _run(loop, repo_seed=RepoSeed(tree_id=seed))
    assert model.calls == []


def test_the_worker_brief_names_the_evaluate_tool_and_not_eval_sh(tmp_path: Path) -> None:
    """The lab has no ``eval.sh``, so the brief tells the agent to use the ``evaluate`` tool.

    Args:
        tmp_path: Pytest's temporary folder.
    """
    config = OptimizeAnythingConfig(engine="autoresearch", run_dir=str(tmp_path), engine_config={"model": ""})
    worker = ar_mod._WorkerAutoResearch(
        config, EngineContext(reflection_lm=str, run_dir=str(tmp_path)), max_tool_calls=100
    )
    worker.example_ids = ["0", "1"]

    brief = worker._brief()

    assert "eval.sh" not in brief
    assert "The `evaluate` tool with only a `file` is a **full evaluation**" in brief
    assert "The `evaluate` tool with a `file` and `example_ids` is a **probe**" in brief
    assert "Only `work/` and `notebook.md` carry over" in brief
    assert "is the only memory" not in brief
    assert (
        "- `notebook.md` is yours. It and `work/` are the only files that survive between rounds, up to size caps: "
        "keep each file at most 8 MiB and all of them together at most 32 MiB, each path inside the lab at most "
        "512 bytes and each file or folder name at most 255 bytes as UTF-8. A file past a cap is dropped." in brief
    )
    # The lab tree is built once per round, so a full evaluation of this round is not in the box's archive yet.
    assert "a full evaluation before this round" in brief
    assert (
        "`archive/` and `frontier/` refresh only between rounds: the `evaluate` result is the current record for "
        "this round." in brief
    )


def test_the_archive_cap_is_64_mib() -> None:
    """The lab tree carries at most 64 MiB of archived candidates."""
    assert ar_mod._MAX_ARCHIVE_BYTES == 64 * 2**20


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
    """With no files proposer, the tool loop is the proposer: a setting that it would drop fails the run before any call.

    Args:
        options: The native options with one refused setting.
        field: The name of the refused setting.
        owner: Alice and her budget generation.
        engine: The database.
        step_log: The step log.
        trees: The tree store.
        tmp_path: Pytest's temporary folder.
    """
    model = _Researcher()
    loop, boxes = _loop(owner, engine, step_log, trees, tmp_path, model, generation=1)

    with pytest.raises(ServiceError, match=rf"AutoResearch .*cannot apply these settings: .*\b{field}\b"):
        _run(loop, native_options=options)
    assert model.calls == []
    assert boxes.opened == 0


def test_a_files_proposer_with_no_repository_is_refused(
    owner: tuple[User, int], engine: Engine, step_log: StepLogStore, trees: TreeStore, tmp_path: Path
) -> None:
    """With no repository the file's command would edit nothing, so its settings fail the run before any call.

    Args:
        owner: Alice and her budget generation.
        engine: The database.
        step_log: The step log.
        trees: The tree store.
        tmp_path: Pytest's temporary folder.
    """
    model = _Researcher()
    loop, boxes = _loop(owner, engine, step_log, trees, tmp_path, model, generation=1)

    with pytest.raises(
        ServiceError, match=r"^AutoResearch in the worker engine loop runs only its built-in proposer, .*run_command"
    ):
        _run(loop, native_options=_command_options())
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
    loop, _ = _loop(owner, engine, step_log, trees, tmp_path, _Researcher(), generation=1)

    result = _run(loop, native_options=_options(budget_route={MCP_RELAY_ROUTE_KEY: {"servers": []}}))

    assert result.best_candidate == "seedxx"


def test_a_task_that_holds_the_metric_source_is_refused(
    owner: tuple[User, int], engine: Engine, step_log: StepLogStore, trees: TreeStore, tmp_path: Path
) -> None:
    """As in the native runtime, a task document that holds the metric source never reaches the lab.

    Args:
        owner: Alice and her budget generation.
        engine: The database.
        step_log: The step log.
        trees: The tree store.
        tmp_path: Pytest's temporary folder.
    """
    model = _Researcher()
    loop, boxes = _loop(owner, engine, step_log, trees, tmp_path, model, generation=1)
    metric = "def score(candidate):\n    return len(candidate)"
    task = Task("seed", objective=f"Maximize this:\n{metric}", cases=list(_CASES), context={"metric_code": metric})

    with pytest.raises(ServiceError, match="metric source"):
        AutoResearchEngine().run(task, loop.server(_score, max_evals=20), loop.context())
    assert model.calls == []
    assert boxes.opened == 0


def test_a_failed_model_call_is_tried_again_and_replays_the_same_way(
    owner: tuple[User, int],
    engine: Engine,
    step_log: StepLogStore,
    trees: TreeStore,
    tmp_path: Path,
    sleeps: list[float],
) -> None:
    """A transient failure commits a failed step and a retry; a resume replays both with no new call and no wait.

    Args:
        owner: Alice and her budget generation.
        engine: The database.
        step_log: The step log.
        trees: The tree store.
        tmp_path: Pytest's temporary folder.
        sleeps: The recorded retry waits.
    """
    first, _ = _loop(
        owner, engine, step_log, trees, tmp_path, _Researcher(fail_at=frozenset({0}), die_at=2), generation=1
    )
    with pytest.raises(_WorkerDiedError):
        _run(first)
    assert [kind for _, kind, _ in _steps(engine)] == ["model", "model", "command", "model"]
    assert sleeps == [loop_tools.MODEL_RETRY_WAIT_SECONDS]

    sleeps.clear()
    _new_generation(engine)
    model = _Researcher()
    loop, _ = _loop(owner, engine, step_log, trees, tmp_path, model, generation=2)
    result = _run(loop)

    assert (result.best_candidate, result.best_score) == ("seedxx", 6.0)
    # The failed step, its retry and the evaluation reply come back from the log; the rest of the run is live.
    assert len(model.calls) == 4
    assert sleeps == []
    steps = _steps(engine)
    round_two = ["model", "command", "model", "model"]
    assert [kind for _, kind, _ in steps] == ["model", "model", "command", "model", "model", *round_two]
    assert [generation for _, _, generation in steps] == [1] * 4 + [2] * 5


def test_a_model_call_that_fails_on_every_attempt_ends_the_session_as_an_error(
    owner: tuple[User, int],
    engine: Engine,
    step_log: StepLogStore,
    trees: TreeStore,
    tmp_path: Path,
    sleeps: list[float],
) -> None:
    """After the fixed number of attempts, the session ends as an error and the run stops with the proposer error.

    Args:
        owner: Alice and her budget generation.
        engine: The database.
        step_log: The step log.
        trees: The tree store.
        tmp_path: Pytest's temporary folder.
        sleeps: The recorded retry waits.
    """
    model = _Researcher(fail_at=frozenset({0, 1, 2}))
    loop, boxes = _loop(owner, engine, step_log, trees, tmp_path, model, generation=1)

    with pytest.raises(ProposerFailedError, match=re.escape(str(ModelCallFailedError("RuntimeError")))):
        _run(loop)
    assert model.attempts == loop_tools.MODEL_ATTEMPTS
    assert [kind for _, kind, _ in _steps(engine)] == ["model"] * loop_tools.MODEL_ATTEMPTS
    assert sleeps == [loop_tools.MODEL_RETRY_WAIT_SECONDS] * (loop_tools.MODEL_ATTEMPTS - 1)
    assert boxes.opened == 0


def test_a_long_command_output_is_cut(
    owner: tuple[User, int], engine: Engine, step_log: StepLogStore, trees: TreeStore, tmp_path: Path
) -> None:
    """A Hebrew output past ``OUTPUT_BYTES`` keeps its head and tail with no escapes, so the prompt stays bounded.

    Args:
        owner: Alice and her budget generation.
        engine: The database.
        step_log: The step log.
        trees: The tree store.
        tmp_path: Pytest's temporary folder.
    """
    big = f"python3 -c \"print('\u05d0' * {loop_tools.OUTPUT_BYTES})\""
    model = _Researcher([_bash(big), *_ROUND])
    loop, _ = _loop(owner, engine, step_log, trees, tmp_path, model, generation=1)

    _run(loop)

    stdout = _tool_result(model, 1)["stdout"]
    assert loop_tools.prompt_bytes(stdout) <= loop_tools.OUTPUT_BYTES
    assert "characters cut ...]" in stdout
    assert "\u05d0" in model.calls[1][-1]["content"]


def test_a_long_evaluate_result_is_cut(
    owner: tuple[User, int], engine: Engine, step_log: StepLogStore, trees: TreeStore, tmp_path: Path
) -> None:
    """An evaluate result past ``OUTPUT_BYTES`` keeps its head and tail, so the next prompt stays bounded.

    Args:
        owner: Alice and her budget generation.
        engine: The database.
        step_log: The step log.
        trees: The tree store.
        tmp_path: Pytest's temporary folder.
    """

    def score(loop: EngineLoop, candidate: Any, case: Any) -> tuple[float | None, dict[str, Any]]:
        """Score by length, with a long side information text.

        Args:
            loop: The engine loop.
            candidate: The candidate text.
            case: Not used.

        Returns:
            The length and the long text.
        """
        del loop, case
        return float(len(str(candidate))), {"log": "z" * loop_tools.OUTPUT_BYTES}

    model = _Researcher()
    loop, _ = _loop(owner, engine, step_log, trees, tmp_path, model, generation=1)

    _run(loop, max_evals=2, stop_at_score=None, score=score)

    result = model.calls[2][-1]
    assert result["role"] == "tool"
    assert loop_tools.prompt_bytes(result["content"]) <= loop_tools.OUTPUT_BYTES
    assert "characters cut ...]" in result["content"]


def test_a_prompt_past_its_cap_stops_the_session_and_the_run_goes_on(
    owner: tuple[User, int],
    engine: Engine,
    step_log: StepLogStore,
    trees: TreeStore,
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """A round whose transcript passes ``MAX_PROMPT_BYTES`` asks the model no more; the next round still runs.

    Args:
        owner: Alice and her budget generation.
        engine: The database.
        step_log: The step log.
        trees: The tree store.
        tmp_path: Pytest's temporary folder.
        monkeypatch: Pytest's monkeypatch.
    """
    monkeypatch.setattr(loop_tools, "MAX_PROMPT_BYTES", 3_000)
    model = _Researcher([*_ROUND, _bash("python3 -c \"print('y' * 3000)\"")], rest=_bash("true"))
    loop, _ = _loop(owner, engine, step_log, trees, tmp_path, model, generation=1)

    result = _run(loop)

    assert (result.best_candidate, result.best_score) == ("seedxx", 6.0)
    assert result.metadata["rounds"] == 2
    assert len(model.calls) == 6
    assert [kind for _, kind, _ in _steps(engine)] == ["model", "command", "model", "model", "command"] * 2


def test_a_prompt_at_its_cap_still_calls_the_model(
    owner: tuple[User, int],
    engine: Engine,
    step_log: StepLogStore,
    trees: TreeStore,
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """With the cap set to each prompt's own size, every prompt sits exactly at ``MAX_PROMPT_BYTES`` and still runs.

    Args:
        owner: Alice and her budget generation.
        engine: The database.
        step_log: The step log.
        trees: The tree store.
        tmp_path: Pytest's temporary folder.
        monkeypatch: Pytest's monkeypatch.
    """
    measure = loop_tools.messages_bytes
    sizes: list[int] = []

    def at_cap(messages: list[dict[str, Any]]) -> int:
        """Measure the prompt with the real measure and move the cap to that size.

        Args:
            messages: The session's messages.

        Returns:
            The prompt's size in UTF-8 bytes.
        """
        size = measure(messages)
        monkeypatch.setattr(loop_tools, "MAX_PROMPT_BYTES", size)
        sizes.append(size)
        return size

    monkeypatch.setattr(loop_tools, "messages_bytes", at_cap)
    model = _Researcher()
    loop, _ = _loop(owner, engine, step_log, trees, tmp_path, model, generation=1)

    result = _run(loop)

    assert (result.best_candidate, result.best_score) == ("seedxx", 6.0)
    assert len(model.calls) == len(sizes) > 0


@pytest.mark.parametrize("bad", [None, math.inf, math.nan])
def test_a_score_that_is_not_finite_never_becomes_the_best(
    bad: float | None, owner: tuple[User, int], engine: Engine, step_log: StepLogStore, trees: TreeStore, tmp_path: Path
) -> None:
    """With no cases, a crashed, infinite or NaN score is no improvement; a later finite score is the best.

    Args:
        bad: The score that is not finite.
        owner: Alice and her budget generation.
        engine: The database.
        step_log: The step log.
        trees: The tree store.
        tmp_path: Pytest's temporary folder.
    """
    loop, _ = _loop(owner, engine, step_log, trees, tmp_path, _Researcher(), generation=1)
    scores = iter([bad, 1.0])
    server = loop.server(lambda _loop, _candidate, _case: (next(scores), {}), max_evals=20)
    adapter = ar_mod._UpstreamServer(server, Task("seed"), loop.context())

    adapter.evaluate("first")
    assert (adapter.best_candidate, adapter.best_score) == (None, None)

    adapter.evaluate("second")
    assert (adapter.best_candidate, adapter.best_score) == ("second", 1.0)


@pytest.mark.parametrize("bad", [math.inf, math.nan])
def test_a_score_that_is_not_finite_neither_stops_the_run_nor_leads_it(
    bad: float, owner: tuple[User, int], engine: Engine, step_log: StepLogStore, trees: TreeStore, tmp_path: Path
) -> None:
    """With no cases, an infinite or NaN first score does not reach ``stop_at_score``; the next finite score wins.

    Args:
        bad: The score that is not finite.
        owner: Alice and her budget generation.
        engine: The database.
        step_log: The step log.
        trees: The tree store.
        tmp_path: Pytest's temporary folder.
    """
    loop, _ = _loop(owner, engine, step_log, trees, tmp_path, _Researcher(), generation=1)

    def score(_loop: EngineLoop, candidate: Any, _case: Any) -> tuple[float | None, dict[str, Any]]:
        """Score the first round's candidate as not finite, and any other by its length.

        Args:
            _loop: The engine loop.
            candidate: The candidate text.
            _case: Not used.

        Returns:
            The score and no side information.
        """
        return (bad if candidate == "seedx" else float(len(candidate))), {}

    result = _run(loop, cases=[], score=score)

    assert (result.best_candidate, result.best_score, result.total_evals) == ("seedxx", 6.0, 2)


def test_an_evaluate_result_keeps_hebrew_with_no_escapes(
    owner: tuple[User, int], engine: Engine, step_log: StepLogStore, trees: TreeStore, tmp_path: Path
) -> None:
    """Hebrew scorer feedback reaches the model as is, so it uses two bytes a letter and not a ``\\u`` escape.

    Args:
        owner: Alice and her budget generation.
        engine: The database.
        step_log: The step log.
        trees: The tree store.
        tmp_path: Pytest's temporary folder.
    """
    model = _Researcher()
    loop, _ = _loop(owner, engine, step_log, trees, tmp_path, model, generation=1)

    _run(
        loop,
        cases=[],
        score=lambda _loop, candidate, _case: (float(len(candidate)), {"note": "\u05e9\u05dc\u05d5\u05dd"}),
    )

    content = model.calls[2][-1]["content"]
    assert "\u05e9\u05dc\u05d5\u05dd" in content
    assert "\\u05e9" not in content
    assert _tool_result(model, 2)["info"]["note"] == "\u05e9\u05dc\u05d5\u05dd"


def test_a_run_with_no_finite_score_fails_and_reports_no_minus_infinity(
    owner: tuple[User, int], engine: Engine, step_log: StepLogStore, trees: TreeStore, tmp_path: Path
) -> None:
    """With no cases and a scorer that always crashes, the run has no best version and fails loudly.

    Args:
        owner: Alice and her budget generation.
        engine: The database.
        step_log: The step log.
        trees: The tree store.
        tmp_path: Pytest's temporary folder.
    """
    loop, _ = _loop(owner, engine, step_log, trees, tmp_path, _Researcher(), generation=1)

    with pytest.raises(RuntimeError, match="without scoring any candidate through the evaluate tool"):
        _run(loop, cases=[], max_evals=2, score=lambda _loop, _candidate, _case: (None, {}))


@pytest.mark.parametrize("bad", [None, math.nan, math.inf])
def test_a_best_version_with_a_crashed_case_has_no_score(
    bad: float | None, owner: tuple[User, int], engine: Engine, step_log: StepLogStore, trees: TreeStore, tmp_path: Path
) -> None:
    """Every version crashes on one case: the result keeps the best filled mean's version, with no score.

    A NaN or infinite case score is a crash, as a metric error is (SKY-751), so no scorer note carries it.

    Args:
        bad: The crashed case's score: a metric error, NaN or infinity.
        owner: Alice and her budget generation.
        engine: The database.
        step_log: The step log.
        trees: The tree store.
        tmp_path: Pytest's temporary folder.
    """

    def score(loop: EngineLoop, candidate: Any, case: Any) -> tuple[float | None, dict[str, Any]]:
        """Crash on the second case, and score the first by length.

        Args:
            loop: The engine loop.
            candidate: The candidate text.
            case: The case.

        Returns:
            The score, or ``bad`` for a crash, and a note.
        """
        del loop
        return (bad if case == _CASES[1] else float(len(str(candidate)))), {"feedback": "note"}

    loop, _ = _loop(owner, engine, step_log, trees, tmp_path, _Researcher(), generation=1)
    events: list[tuple[str, dict[str, Any]]] = []

    result = _run(loop, events=events, max_evals=4, stop_at_score=None, score=score)

    assert result.best_candidate == "seedxx"
    assert result.best_score is None
    assert [(data["score"], data["per_example"]) for kind, data in events if kind == PROGRESS_CANDIDATE] == [
        (None, []),
        (None, []),
    ]
    notes = [data["score"] for kind, data in events if kind == PROGRESS_MINIBATCH]
    assert notes == [5.0, 6.0]


def test_a_version_with_a_metric_error_on_every_case_shows_with_no_score_and_is_not_the_pick(
    owner: tuple[User, int], engine: Engine, step_log: StepLogStore, trees: TreeStore, tmp_path: Path
) -> None:
    """Round one's version is a metric error on every case: it shows as a candidate with no score (SKY-825).

    Round two's version scores, so it is the final pick.

    Args:
        owner: Alice and her budget generation.
        engine: The database.
        step_log: The step log.
        trees: The tree store.
        tmp_path: Pytest's temporary folder.
    """

    def score(loop: EngineLoop, candidate: Any, case: Any) -> tuple[float | None, dict[str, Any]]:
        """Give round one's version a metric error on every case, and score any other by its length.

        Args:
            loop: The engine loop.
            candidate: The candidate text.
            case: Not used.

        Returns:
            The score, or ``None`` for a metric error, and no side information.
        """
        del loop, case
        return (None if candidate == "seedx" else float(len(str(candidate)))), {}

    loop, _ = _loop(owner, engine, step_log, trees, tmp_path, _Researcher(), generation=1)
    events: list[tuple[str, dict[str, Any]]] = []

    result = _run(loop, events=events, max_evals=4, stop_at_score=None, score=score)

    assert [data["score"] for kind, data in events if kind == PROGRESS_CANDIDATE] == [None, 6.0]
    assert (result.best_candidate, result.best_score) == ("seedxx", 6.0)


def test_a_repeated_case_id_is_scored_once_and_the_mean_is_over_distinct_cases(
    owner: tuple[User, int], engine: Engine, step_log: StepLogStore, trees: TreeStore, tmp_path: Path
) -> None:
    """A sweep that names a case twice spends one call on it, and logs the mean that the ranking reads.

    Args:
        owner: Alice and her budget generation.
        engine: The database.
        step_log: The step log.
        trees: The tree store.
        tmp_path: Pytest's temporary folder.
    """

    def score(loop: EngineLoop, candidate: Any, case: Any) -> tuple[float | None, dict[str, Any]]:
        """Score the first case by length and the second as zero.

        Args:
            loop: The engine loop.
            candidate: The candidate text.
            case: The case.

        Returns:
            The score and no side information.
        """
        del loop
        return (0.0 if case == _CASES[1] else float(len(str(candidate)))), {}

    model = _Researcher([_WRITE, _evaluate(example_ids=["0", "0", "1"])])
    loop, _ = _loop(owner, engine, step_log, trees, tmp_path, model, generation=1)
    events: list[tuple[str, dict[str, Any]]] = []

    result = _run(loop, events=events, max_evals=4, stop_at_score=None, score=score)

    first = _tool_result(model, 2)
    assert (first["average_score"], first["scores"], first["num_evaluated"]) == (2.5, {"0": 5.0, "1": 0.0}, 2)
    assert first["budget"] == {"used": 2, "remaining": 2}
    assert [data["score"] for kind, data in events if kind == PROGRESS_CANDIDATE] == [2.5, 3.0]
    assert (result.best_candidate, result.best_score, result.total_evals) == ("seedxx", 3.0, 4)


def test_a_file_the_agent_deletes_does_not_come_back_from_an_earlier_round(
    owner: tuple[User, int], engine: Engine, step_log: StepLogStore, trees: TreeStore, tmp_path: Path
) -> None:
    """The local ``work/`` and ``notebook.md`` follow the tree: a file the agent removed is gone after its round.

    Args:
        owner: Alice and her budget generation.
        engine: The database.
        step_log: The step log.
        trees: The tree store.
        tmp_path: Pytest's temporary folder.
    """
    model = _Researcher([_bash("rm -f notebook.md work/seed.txt && printf %s seed{x} > work/c{n}.txt"), _evaluate()])
    loop, _ = _loop(owner, engine, step_log, trees, tmp_path, model, generation=1)

    _run(loop)

    lab = _lab(tmp_path)
    assert not (lab / "notebook.md").exists()
    assert sorted(path.name for path in (lab / "work").iterdir()) == ["c1.txt", "c2.txt"]


def test_a_file_past_the_candidate_cap_gets_an_error_result_and_spends_no_budget(
    owner: tuple[User, int],
    engine: Engine,
    step_log: StepLogStore,
    trees: TreeStore,
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """A lab file larger than the candidate cap is refused before it becomes a candidate; a file at the cap scores.

    Args:
        owner: Alice and her budget generation.
        engine: The database.
        step_log: The step log.
        trees: The tree store.
        tmp_path: Pytest's temporary folder.
        monkeypatch: Pytest's monkeypatch.
    """
    monkeypatch.setattr(native_engines, "MAX_CANDIDATE_BYTES", 5)
    big: Turn = lambda n: {"id": "big", "name": "evaluate", "arguments": {"file": "work/big.txt"}}  # noqa: E731
    model = _Researcher([_WRITE, _bash("printf %s seedxx > work/big.txt"), big, _evaluate()])
    loop, _ = _loop(owner, engine, step_log, trees, tmp_path, model, generation=1)

    result = _run(loop, max_evals=2, stop_at_score=None)

    assert "larger than 5 bytes" in _tool_result(model, 3)["error"]
    assert _tool_result(model, 4)["budget"] == {"used": 2, "remaining": 0}
    assert (result.best_candidate, result.best_score, result.total_evals) == ("seedx", 5.0, 2)


def test_an_over_cap_lab_file_never_loads_for_evaluate_or_the_carried_lab(
    owner: tuple[User, int],
    engine: Engine,
    step_log: StepLogStore,
    trees: TreeStore,
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """``evaluate`` and the carried lab read an over-cap file with the candidate cap, so its bytes never load.

    Args:
        owner: Alice and her budget generation.
        engine: The database.
        step_log: The step log.
        trees: The tree store.
        tmp_path: Pytest's temporary folder.
        monkeypatch: Pytest's monkeypatch.
    """
    monkeypatch.setattr(native_engines, "MAX_CANDIDATE_BYTES", 5)
    reads: list[tuple[str, int | None, bytes | None | FileTooLargeError]] = []
    read_file = EngineLoop.read_file

    def recorded(loop: EngineLoop, tree_id: str, path: str, max_bytes: int | None = None) -> bytes | None:
        """Record each read's path, its cap and what it gave.

        Args:
            loop: The engine loop.
            tree_id: The tree.
            path: The file path.
            max_bytes: The read's cap.

        Returns:
            The file bytes, or ``None``.
        """
        try:
            data = read_file(loop, tree_id, path, max_bytes)
        except FileTooLargeError as error:
            reads.append((path, max_bytes, error))
            raise
        reads.append((path, max_bytes, data))
        return data

    monkeypatch.setattr(EngineLoop, "read_file", recorded)
    big: Turn = lambda n: {"id": "big", "name": "evaluate", "arguments": {"file": "work/big.txt"}}  # noqa: E731
    model = _Researcher([_WRITE, _bash("printf %s seedxx > work/big.txt"), big, _evaluate()])
    loop, _ = _loop(owner, engine, step_log, trees, tmp_path, model, generation=1)

    _run(loop, max_evals=2, stop_at_score=None)

    big_reads = [(cap, type(outcome)) for path, cap, outcome in reads if path == "work/big.txt"]
    # One read for ``evaluate`` and one for the carried lab after the round.
    assert big_reads == [(5, FileTooLargeError)] * 2
    assert not (_lab(tmp_path) / "work" / "big.txt").exists()


@pytest.mark.parametrize(
    "arguments",
    [
        '{"file": ' + "[" * (loop_tools.MAX_ARGUMENT_DEPTH + 1) + "]" * (loop_tools.MAX_ARGUMENT_DEPTH + 1) + "}",
        {
            "file": "work/c1.txt",
            "pad": functools.reduce(lambda inner, _: [inner], range(loop_tools.MAX_ARGUMENT_DEPTH), 0),
        },
        {"file": "work/c1.txt", "pad": "x" * loop_tools.MAX_ARGUMENT_CHARS},
        '{"file": "' + "x" * loop_tools.MAX_ARGUMENT_CHARS + '"}',
    ],
    ids=["deep-text", "deep-mapping", "long-mapping", "long-text"],
)
def test_evaluate_arguments_past_a_bound_get_the_bound_error(
    owner: tuple[User, int],
    engine: Engine,
    step_log: StepLogStore,
    trees: TreeStore,
    tmp_path: Path,
    arguments: Any,
) -> None:
    """Refused evaluate arguments get the bound error, not the path error, and the transcript holds ``{}``.

    No case keeps its text: each breaks the length or the depth bound, as text or as a mapping.

    Args:
        owner: Alice and her budget generation.
        engine: The database.
        step_log: The step log.
        trees: The tree store.
        tmp_path: Pytest's temporary folder.
        arguments: The arguments past a bound, as JSON text or as a mapping.
    """
    bad: Turn = lambda n: {"id": "bad", "name": "evaluate", "arguments": arguments}  # noqa: E731
    model = _Researcher([_WRITE, bad, _evaluate()])
    loop, _ = _loop(owner, engine, step_log, trees, tmp_path, model, generation=1)

    result = _run(loop, max_evals=2, stop_at_score=None)

    error = _tool_result(model, 2)["error"]
    assert error.startswith("Send the evaluate tool arguments as JSON")
    assert f"nested at most {loop_tools.MAX_ARGUMENT_DEPTH} levels" in error
    transcript = model.calls[2][-2]["tool_calls"][0]["function"]["arguments"]
    assert transcript == "{}"
    assert (result.best_candidate, result.total_evals) == ("seedx", 2)


def test_the_carried_lab_skips_files_past_the_caps_the_same_way_on_a_resume(
    owner: tuple[User, int],
    engine: Engine,
    step_log: StepLogStore,
    trees: TreeStore,
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """A file past the per-file cap, and the files past the total cap in sorted order, stay in the tree only.

    Args:
        owner: Alice and her budget generation.
        engine: The database.
        step_log: The step log.
        trees: The tree store.
        tmp_path: Pytest's temporary folder.
        monkeypatch: Pytest's monkeypatch.
    """
    monkeypatch.setattr(native_engines, "MAX_CANDIDATE_BYTES", 11)
    monkeypatch.setattr(ar_mod, "_MAX_CARRY_BYTES", 27)
    # In order: the notebook (11 bytes), 0 (12, past the per-file cap; it would fit the total cap), a (6, at 17),
    # c (6, at 23), d (6, past the total cap) and seed.txt (4, at 27). A skip does not stop the scan.
    files = "printf 123456789012 > work/0 && printf 123456 > work/a && printf 123456 > work/c && printf 123456 > work/d"
    model = _Researcher([_bash(f"printf '## Round {{n}}\\n' > notebook.md && {files}")])
    loop, _ = _loop(owner, engine, step_log, trees, tmp_path, model, generation=1)

    with pytest.raises(RuntimeError, match="without scoring any candidate through the evaluate tool"):
        _run(loop)

    lab = _lab(tmp_path)
    assert sorted(path.name for path in (lab / "work").iterdir()) == ["a", "c", "seed.txt"]
    assert (lab / "notebook.md").read_text() == "## Round 1\n"

    # The resume replays the committed steps, so the model is never called.
    loop, _ = _loop(owner, engine, step_log, trees, tmp_path, _Researcher(die_at=0), generation=2)
    with pytest.raises(RuntimeError, match="without scoring any candidate through the evaluate tool"):
        _run(loop)

    assert sorted(path.name for path in (_lab(tmp_path, 2) / "work").iterdir()) == ["a", "c", "seed.txt"]


def test_the_lab_tree_carries_the_best_archived_candidates_up_to_the_archive_cap(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """The archive in the lab tree is best first, newest first on a tie, under the cap; skipped files are not read.

    Args:
        tmp_path: Pytest's temporary folder.
        monkeypatch: Pytest's monkeypatch.
    """
    monkeypatch.setattr(ar_mod, "_MAX_ARCHIVE_BYTES", 10)
    config = OptimizeAnythingConfig(engine="autoresearch", run_dir=str(tmp_path), engine_config={"model": ""})
    worker = ar_mod._WorkerAutoResearch(
        config, EngineContext(reflection_lm=str, run_dir=str(tmp_path)), max_tool_calls=100
    )
    (worker.work_dir / "archive").mkdir(parents=True)
    (worker.work_dir / "archive" / "index.tsv").write_text("id\tscore\tround\n", encoding="utf-8")
    (worker.work_dir / "notebook.md").write_text("notes", encoding="utf-8")
    # In order of the walk: c005 (11 bytes, past the cap alone), the 3.0 tie newest first: c006 (at 4), c004 (at
    # 8), c002 (past the cap), then c001 (at 10) and last c003, a crash at -inf (past the cap).
    for text, score in [("aa", 1.0), ("bbbb", 3.0), ("cc", -math.inf), ("dddd", 3.0), ("e" * 11, 9.0), ("ffff", 3.0)]:
        worker._observe(native_engines.Observation(text, score, {}, {}, True, 1))
    reads: list[str] = []
    read_bytes = Path.read_bytes

    def recorded(path: Path) -> bytes:
        """Record each local file read.

        Args:
            path: The file.

        Returns:
            The file bytes.
        """
        reads.append(path.relative_to(worker.work_dir).as_posix())
        return read_bytes(path)

    monkeypatch.setattr(Path, "read_bytes", recorded)

    files = worker._lab_files()

    archived = {name: data for name, data in files.items() if name.startswith("archive/c")}
    assert archived == {"archive/c006.txt": b"ffff", "archive/c004.txt": b"dddd", "archive/c001.txt": b"aa"}
    assert sum(map(len, archived.values())) <= ar_mod._MAX_ARCHIVE_BYTES
    assert {"archive/index.tsv", "notebook.md"} <= files.keys()
    assert sorted(reads) == sorted(files)
    # The worker's own archive stays complete.
    assert len(list((worker.work_dir / "archive").glob("c*.txt"))) == 6


def test_the_carried_lab_reads_each_file_with_at_most_the_bytes_left_under_the_total_cap(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """A read after the total cap is spent asks for at most the bytes left, so a file that cannot fit never loads.

    Args:
        tmp_path: Pytest's temporary folder.
        monkeypatch: Pytest's monkeypatch.
    """
    monkeypatch.setattr(native_engines, "MAX_CANDIDATE_BYTES", 10)
    monkeypatch.setattr(ar_mod, "_MAX_CARRY_BYTES", 15)
    tree = {"work/a": b"a" * 10, "work/b": b"b" * 4, "work/c": b"c" * 3}
    reads: list[tuple[str, int | None]] = []

    def read_file(tree_id: str, path: str, max_bytes: int | None = None) -> bytes | None:
        """Read a fake tree file, and record the cap of the read.

        Args:
            tree_id: Not used.
            path: The file path.
            max_bytes: The cap of the read.

        Returns:
            The file bytes, or ``None`` when the file is not in the tree.

        Raises:
            FileTooLargeError: When the file is longer than the cap.
        """
        del tree_id
        reads.append((path, max_bytes))
        data = tree.get(path)
        if data is not None and max_bytes is not None and len(data) > max_bytes:
            raise FileTooLargeError(path, len(data), max_bytes)
        return data

    config = OptimizeAnythingConfig(engine="autoresearch", run_dir=str(tmp_path), engine_config={"model": ""})
    ctx = EngineContext(
        reflection_lm=str,
        run_dir=str(tmp_path),
        read_file=read_file,
        list_files=lambda tree_id, path="", executable=False: [] if executable else sorted(tree),
    )
    worker = ar_mod._WorkerAutoResearch(config, ctx, max_tool_calls=100)
    (worker.work_dir / "work").mkdir(parents=True, exist_ok=True)

    worker._pull("tree")

    # The notebook is missing; a (10) fits, b (4) fits the 5 bytes left, c (3) is past the 1 byte left.
    assert reads == [("notebook.md", 10), ("work/a", 10), ("work/b", 5), ("work/c", 1)]
    assert sorted(path.name for path in (worker.work_dir / "work").iterdir()) == ["a", "b"]


def test_a_run_writes_a_lab_tree_with_the_archive_under_its_cap(
    owner: tuple[User, int],
    engine: Engine,
    step_log: StepLogStore,
    trees: TreeStore,
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Each round's tree holds the best archived candidates that fit the cap; the run still finds the best one.

    Args:
        owner: Alice and her budget generation.
        engine: The database.
        step_log: The step log.
        trees: The tree store.
        tmp_path: Pytest's temporary folder.
        monkeypatch: Pytest's monkeypatch.
    """
    monkeypatch.setattr(ar_mod, "_MAX_ARCHIVE_BYTES", 13)
    writes: list[dict[str, bytes]] = []
    put_files = EngineLoop.put_files

    def recorded(loop: EngineLoop, files: dict[str, bytes], base_tree_id: str | None = None, **options: Any) -> str:
        """Record each tree write.

        Args:
            loop: The engine loop.
            files: The file bytes by path.
            base_tree_id: The base tree.
            **options: The other write options, such as ``executables``.

        Returns:
            The new tree's id.
        """
        writes.append(dict(files))
        return put_files(loop, files, base_tree_id, **options)

    monkeypatch.setattr(EngineLoop, "put_files", recorded)
    loop, _ = _loop(owner, engine, step_log, trees, tmp_path, _Researcher(), generation=1)

    result = _run(loop, max_evals=8, stop_at_score=None)

    # Round 4 starts with seedx (c001, 5 bytes), seedxx (c002, 6) and seedxxx (c003, 7) in the archive.
    archived = {name: data for name, data in writes[3].items() if name.startswith("archive/c")}
    assert archived == {"archive/c003.txt": b"seedxxx", "archive/c002.txt": b"seedxx"}
    assert len(list((_lab(tmp_path) / "archive").glob("c*.txt"))) == 4
    assert (result.best_candidate, result.best_score, result.total_evals) == ("seedxxxx", 8.0, 8)


def test_an_empty_lab_folder_gets_a_keep_marker_that_never_comes_back(
    owner: tuple[User, int],
    engine: Engine,
    step_log: StepLogStore,
    trees: TreeStore,
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Round 1's empty ``frontier/`` and a ``work/`` with every file dropped stay in the box tree as ``.keep``.

    The marker is not the agent's file, so ``_pull`` leaves it in the tree.

    Args:
        owner: Alice and her budget generation.
        engine: The database.
        step_log: The step log.
        trees: The tree store.
        tmp_path: Pytest's temporary folder.
        monkeypatch: Pytest's monkeypatch.
    """
    writes: list[dict[str, bytes]] = []
    put_files = EngineLoop.put_files

    def recorded(loop: EngineLoop, files: dict[str, bytes], base_tree_id: str | None = None, **options: Any) -> str:
        """Record each tree write.

        Args:
            loop: The engine loop.
            files: The file bytes by path.
            base_tree_id: The base tree.
            **options: The other write options, such as ``executables``.

        Returns:
            The new tree's id.
        """
        writes.append(dict(files))
        return put_files(loop, files, base_tree_id, **options)

    monkeypatch.setattr(EngineLoop, "put_files", recorded)
    model = _Researcher([*_ROUND, _bash("rm -f work/*")])
    loop, _ = _loop(owner, engine, step_log, trees, tmp_path, model, generation=1)

    result = _run(loop, max_evals=6, stop_at_score=None)

    assert writes[0]["frontier/.keep"] == b""
    assert [name for name in writes[1] if name.startswith("work/")] == ["work/.keep"]
    assert [name for name in writes[2] if name.startswith("work/")] == ["work/.keep"]
    assert not (_lab(tmp_path) / "work" / ".keep").exists()
    assert (result.best_candidate, result.total_evals) == ("seedxxx", 6)


def test_a_lab_file_the_local_file_system_refuses_stays_behind(
    owner: tuple[User, int],
    engine: Engine,
    step_log: StepLogStore,
    trees: TreeStore,
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """A path that the local file system refuses stays in the tree only, and the run goes on to score.

    Args:
        owner: Alice and her budget generation.
        engine: The database.
        step_log: The step log.
        trees: The tree store.
        tmp_path: Pytest's temporary folder.
        monkeypatch: Pytest's monkeypatch.
    """
    write_bytes = Path.write_bytes

    def refuse_long(path: Path, data: bytes) -> int:
        """Refuse the file named ``long`` as a path past the name length is refused, and write the others.

        Args:
            path: The local file.
            data: The bytes.

        Returns:
            The number of bytes written.

        Raises:
            OSError: For the file named ``long``.
        """
        if path.name == "long":
            raise OSError(errno.ENAMETOOLONG, "File name too long", str(path))
        return write_bytes(path, data)

    # The local boxes share the file system of the lab, so a real long path would fail in the box first.
    monkeypatch.setattr(Path, "write_bytes", refuse_long)
    model = _Researcher([_bash("printf x > work/long"), *_ROUND])
    loop, _ = _loop(owner, engine, step_log, trees, tmp_path, model, generation=1)

    result = _run(loop)

    assert sorted(path.name for path in (_lab(tmp_path) / "work").iterdir()) == ["c1.txt", "c2.txt", "seed.txt"]
    assert result.best_candidate == "seedxx"


def test_an_over_cap_file_at_a_path_the_local_file_system_refuses_does_not_fail_the_run(
    owner: tuple[User, int],
    engine: Engine,
    step_log: StepLogStore,
    trees: TreeStore,
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """A skipped file whose local path the worker refuses, as on a deep run_dir, stays behind and the run scores.

    Args:
        owner: Alice and her budget generation.
        engine: The database.
        step_log: The step log.
        trees: The tree store.
        tmp_path: Pytest's temporary folder.
        monkeypatch: Pytest's monkeypatch.
    """
    monkeypatch.setattr(native_engines, "MAX_CANDIDATE_BYTES", 11)
    unlink = Path.unlink

    def refuse_long(path: Path, missing_ok: bool = False) -> None:
        """Refuse the file named ``long`` as a path past the name length is refused, and unlink the others.

        Args:
            path: The local file.
            missing_ok: Whether a missing file is fine.

        Raises:
            OSError: For the file named ``long``.
        """
        if path.name == "long":
            raise OSError(errno.ENAMETOOLONG, "File name too long", str(path))
        unlink(path, missing_ok=missing_ok)

    # The local boxes share the file system of the lab, so a real long path would fail in the box first.
    monkeypatch.setattr(Path, "unlink", refuse_long)
    model = _Researcher([_bash("printf 123456789012 > work/long"), *_ROUND])
    loop, _ = _loop(owner, engine, step_log, trees, tmp_path, model, generation=1)

    result = _run(loop)

    assert sorted(path.name for path in (_lab(tmp_path) / "work").iterdir()) == ["c1.txt", "c2.txt", "seed.txt"]
    assert result.best_candidate == "seedxx"


@pytest.mark.parametrize("prefix", ["", "p" * 250 + "/" + "q" * 250])
def test_a_tree_path_past_the_fixed_bounds_stays_behind_whatever_the_local_prefix(
    prefix: str, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """The byte bounds on the relative path decide each skip before any read, so a deep run_dir skips the same files.

    Args:
        prefix: Extra folders in front of the run_dir; the long one makes the local file system refuse long paths.
        tmp_path: Pytest's temporary folder.
        monkeypatch: Pytest's monkeypatch.
    """
    monkeypatch.setattr(ar_mod, "_MAX_CARRY_BYTES", 3)
    name = "n" * ar_mod._MAX_CARRY_NAME_BYTES
    # Each path of the tree in sorted order: at the path bound, past it by one byte, a name past the name bound, a
    # short file that a local refusal before it must not let past the total cap, and a short file under it.
    at_bound = f"work/{name}/" + "m" * (ar_mod._MAX_CARRY_PATH_BYTES - len(f"work/{name}/"))
    past_path = at_bound + "m"
    past_name = f"work/o{name}"
    tree = {
        "notebook.md": b"",
        at_bound: b"ab",
        past_path: b"x",
        past_name: b"x",
        "work/zz": b"cd",
        "work/zzz": b"e",
    }
    assert len(at_bound.encode()) == ar_mod._MAX_CARRY_PATH_BYTES
    reads: list[str] = []

    def read_file(tree_id: str, path: str, max_bytes: int | None = None) -> bytes | None:
        """Read a file of the fake tree and record the path.

        Args:
            tree_id: The tree.
            path: The file.
            max_bytes: The cap.

        Returns:
            The bytes.

        Raises:
            FileTooLargeError: When the file is longer than the cap, as ``TreeStore.get_blob`` does.
        """
        reads.append(path)
        data = tree[path]
        if max_bytes is not None and len(data) > max_bytes:
            raise FileTooLargeError(path, len(data), max_bytes)
        return data

    def list_files(tree_id: str, path: str = "", executable: bool = False) -> list[str] | None:
        """List the files of the fake tree under ``work/``.

        Args:
            tree_id: The tree.
            path: The folder.
            executable: List only the executables; the fake tree has none.

        Returns:
            The sorted paths.
        """
        return [] if executable else sorted(name for name in tree if name.startswith("work/"))

    run_dir = tmp_path / prefix if prefix else tmp_path
    config = OptimizeAnythingConfig(engine="autoresearch", run_dir=str(run_dir), engine_config={"model": ""})
    ctx = EngineContext(reflection_lm=str, run_dir=str(run_dir), read_file=read_file, list_files=list_files)
    worker = ar_mod._WorkerAutoResearch(config, ctx, max_tool_calls=100)
    (worker.work_dir / "work").mkdir(parents=True)
    write_bytes = Path.write_bytes

    def write(path: Path, data: bytes) -> int:
        """Refuse the path at the bound when the run_dir is deep, as the local file system does, and write the others.

        Args:
            path: The local file.
            data: The bytes.

        Returns:
            The number of bytes written.

        Raises:
            OSError: For a path past PATH_MAX.
        """
        if len(str(path).encode()) >= 1024:
            raise OSError(errno.ENAMETOOLONG, "File name too long", str(path))
        return write_bytes(path, data)

    monkeypatch.setattr(Path, "write_bytes", write)

    worker._pull("tree")

    assert reads == ["notebook.md", at_bound, "work/zz", "work/zzz"]
    kept = sorted(path.relative_to(worker.work_dir).as_posix() for path in worker.work_dir.rglob("*") if path.is_file())
    # The path at the bound takes 2 of the 3 carried bytes even when the local file system refuses it.
    assert kept == (["notebook.md", "work/zzz"] if prefix else ["notebook.md", at_bound, "work/zzz"])


def test_a_model_call_that_fails_after_a_tool_call_ends_only_the_round(
    owner: tuple[User, int], engine: Engine, step_log: StepLogStore, trees: TreeStore, tmp_path: Path
) -> None:
    """Tool output can push the prompt past the model's context, so the failure ends the round and the run goes on.

    Args:
        owner: Alice and her budget generation.
        engine: The database.
        step_log: The step log.
        trees: The tree store.
        tmp_path: Pytest's temporary folder.
    """
    model = _Researcher(fail_at=frozenset({2, 3, 4}))
    loop, _ = _loop(owner, engine, step_log, trees, tmp_path, model, generation=1)

    result = _run(loop)

    assert (result.best_candidate, result.best_score) == ("seedxx", 6.0)
    assert result.metadata["rounds"] == 2
    round_one = ["model", "command", "model", *["model"] * loop_tools.MODEL_ATTEMPTS]
    assert [kind for _, kind, _ in _steps(engine)] == [*round_one, "model", "command", "model", "model"]


def test_billed_failed_calls_that_reach_the_cost_cap_stop_the_retries_as_out_of_budget(
    owner: tuple[User, int], engine: Engine, step_log: StepLogStore, trees: TreeStore, tmp_path: Path
) -> None:
    """Two replies and two billed failures reach a cap of four cents: no third attempt, and no proposer error.

    Args:
        owner: Alice and her budget generation.
        engine: The database.
        step_log: The step log.
        trees: The tree store.
        tmp_path: Pytest's temporary folder.
    """
    model = _Researcher(billed_at=frozenset({2, 3, 4}))
    loop, _ = _loop(owner, engine, step_log, trees, tmp_path, model, generation=1)

    result = _run(loop, native_options=_options(max_token_cost=0.04))

    assert model.attempts == 4
    assert [kind for _, kind, _ in _steps(engine)] == ["model", "command", "model", "model", "model"]
    assert result.best_candidate == "seedx"
    assert result.metadata["proposer_cost_usd"] == 0.04
    assert result.metadata["rounds"] == 1


def test_a_command_past_its_byte_cap_gets_an_error_result_and_opens_no_box(
    owner: tuple[User, int], engine: Engine, step_log: StepLogStore, trees: TreeStore, tmp_path: Path
) -> None:
    """A command over ``MAX_COMMAND_BYTES`` as UTF-8, though under the argument cap in characters, never runs.

    Args:
        owner: Alice and her budget generation.
        engine: The database.
        step_log: The step log.
        trees: The tree store.
        tmp_path: Pytest's temporary folder.
    """
    long = "true # " + "א" * (loop_tools.MAX_COMMAND_BYTES // 2)
    model = _Researcher([lambda n: {"id": "long", "name": "bash", "arguments": {"command": long}}, *_ROUND])
    loop, boxes = _loop(owner, engine, step_log, trees, tmp_path, model, generation=1)

    _run(loop)

    assert _tool_result(model, 1)["error"].startswith(f"Keep the command at most {loop_tools.MAX_COMMAND_BYTES} bytes")
    assert [kind for _, kind, _ in _steps(engine)][:3] == ["model", "model", "command"]
    assert boxes.opened == 2


@pytest.mark.parametrize("extra", [0, 1])
def test_a_command_at_its_byte_cap_runs_and_one_byte_more_is_refused(
    extra: int, owner: tuple[User, int], engine: Engine, step_log: StepLogStore, trees: TreeStore, tmp_path: Path
) -> None:
    """A command of exactly ``MAX_COMMAND_BYTES`` as UTF-8 runs in a box; one byte more gets the error result.

    Args:
        extra: The bytes past the cap.
        owner: Alice and her budget generation.
        engine: The database.
        step_log: The step log.
        trees: The tree store.
        tmp_path: Pytest's temporary folder.
    """
    head = "true # " + "א" * 1_000
    size = loop_tools.MAX_COMMAND_BYTES + extra
    long = head + "x" * (size - len(head.encode("utf-8")))
    assert len(long.encode("utf-8")) == size
    model = _Researcher([lambda n: {"id": "long", "name": "bash", "arguments": {"command": long}}, *_ROUND])
    loop, boxes = _loop(owner, engine, step_log, trees, tmp_path, model, generation=1)

    _run(loop)

    result = _tool_result(model, 1)
    if extra:
        assert result["error"].startswith(f"Keep the command at most {loop_tools.MAX_COMMAND_BYTES} bytes")
        assert [kind for _, kind, _ in _steps(engine)][:3] == ["model", "model", "command"]
    else:
        assert result["exit_code"] == 0
        assert [kind for _, kind, _ in _steps(engine)][:3] == ["model", "command", "model"]
    # Each of the two rounds opens a box for the write, and one more for the long command when it runs.
    assert boxes.opened == (2 if extra else 4)


def test_one_reply_past_the_tool_call_cap_runs_only_the_calls_under_the_cap(
    owner: tuple[User, int], engine: Engine, step_log: StepLogStore, trees: TreeStore, tmp_path: Path
) -> None:
    """A reply with two calls more than ``max_tool_calls`` commits ``max_tool_calls`` commands, then stops.

    Args:
        owner: Alice and her budget generation.
        engine: The database.
        step_log: The step log.
        trees: The tree store.
        tmp_path: Pytest's temporary folder.
    """
    calls = [{"id": f"b{i}", "name": "bash", "arguments": {"command": "true"}} for i in range(5)]
    many: Turn = lambda n: calls  # noqa: E731
    model = _Researcher([many])
    loop, boxes = _loop(owner, engine, step_log, trees, tmp_path, model, generation=1)

    with pytest.raises(RuntimeError, match="without scoring any candidate through the evaluate tool"):
        _run(loop, native_options=_options(max_tool_calls=3))

    assert len(model.calls) == 1
    assert boxes.opened == 3
    assert [kind for _, kind, _ in _steps(engine)] == ["model", "command", "command", "command"]


def test_an_argument_at_the_depth_bound_is_read_and_one_past_it_is_refused() -> None:
    """Arguments nested exactly ``MAX_ARGUMENT_DEPTH`` levels are read; one level more is refused."""
    at_bound = {"pad": functools.reduce(lambda inner, _: [inner], range(loop_tools.MAX_ARGUMENT_DEPTH - 1), 0)}
    past_bound = {"pad": [at_bound["pad"]]}

    assert loop_tools.arguments({"arguments": at_bound}) == at_bound
    assert loop_tools.refused_arguments({"arguments": at_bound}, "bash") is None
    assert loop_tools.arguments({"arguments": past_bound}) is None
    assert loop_tools.refused_arguments({"arguments": past_bound}, "bash") is not None


def test_an_output_of_exactly_the_cut_limit_is_not_cut() -> None:
    """An output whose prompt bytes equal ``OUTPUT_BYTES`` stays whole; one byte more gets cut."""
    quotes = loop_tools.prompt_bytes("")
    exact = "a" * (loop_tools.OUTPUT_BYTES - quotes)

    assert loop_tools.prompt_bytes(exact) == loop_tools.OUTPUT_BYTES
    assert loop_tools.clip(exact) == exact
    assert "characters cut ...]" in loop_tools.clip(exact + "a")


def test_a_tie_on_the_best_score_keeps_the_first_candidate(
    owner: tuple[User, int], engine: Engine, step_log: StepLogStore, trees: TreeStore, tmp_path: Path
) -> None:
    """With no cases, a later candidate with the same score does not replace the best one.

    Args:
        owner: Alice and her budget generation.
        engine: The database.
        step_log: The step log.
        trees: The tree store.
        tmp_path: Pytest's temporary folder.
    """
    loop, _ = _loop(owner, engine, step_log, trees, tmp_path, _Researcher(), generation=1)
    server = loop.server(lambda _loop, _candidate, _case: (1.0, {}), max_evals=20)
    adapter = ar_mod._UpstreamServer(server, Task("seed"), loop.context())

    adapter.evaluate("first")
    adapter.evaluate("second")

    assert (adapter.best_candidate, adapter.best_score) == ("first", 1.0)


def test_a_stop_during_a_command_step_ends_the_run(
    owner: tuple[User, int], engine: Engine, step_log: StepLogStore, trees: TreeStore, tmp_path: Path
) -> None:
    """A user or budget stop in a command goes up to the run, and no later model step commits.

    Args:
        owner: Alice and her budget generation.
        engine: The database.
        step_log: The step log.
        trees: The tree store.
        tmp_path: Pytest's temporary folder.
    """

    def stop(tree: str, command: str, env: Any) -> Any:
        """Stop the loop as a user stop or a refused budget does.

        Args:
            tree: Not used.
            command: Not used.
            env: Not used.

        Raises:
            LoopStoppedError: Always.
        """
        del tree, command, env
        raise LoopStoppedError("The user stopped the run.")

    model = _Researcher()
    loop, _ = _loop(owner, engine, step_log, trees, tmp_path, model, generation=1)
    ctx = dataclasses.replace(loop.context(stop_at_score=6.0), run_command=stop)

    with pytest.raises(LoopStoppedError, match="The user stopped the run"):
        AutoResearchEngine().run(Task("seed", cases=list(_CASES)), loop.server(_score, max_evals=20), ctx)

    assert len(model.calls) == 1
    assert [kind for _, kind, _ in _steps(engine)] == ["model"]


def test_a_stop_during_an_evaluate_step_ends_the_run(
    owner: tuple[User, int], engine: Engine, step_log: StepLogStore, trees: TreeStore, tmp_path: Path
) -> None:
    """A user or budget stop in the scorer goes up to the run, not to the agent as the budget marker.

    Args:
        owner: Alice and her budget generation.
        engine: The database.
        step_log: The step log.
        trees: The tree store.
        tmp_path: Pytest's temporary folder.
    """

    def stop(loop: EngineLoop, candidate: Any, case: Any) -> tuple[float | None, dict[str, Any]]:
        """Stop the loop as a user stop or a refused budget does.

        Args:
            loop: Not used.
            candidate: Not used.
            case: Not used.

        Raises:
            LoopStoppedError: Always.
        """
        del loop, candidate, case
        raise LoopStoppedError("The user stopped the run.")

    model = _Researcher()
    loop, _ = _loop(owner, engine, step_log, trees, tmp_path, model, generation=1)

    with pytest.raises(LoopStoppedError, match="The user stopped the run"):
        _run(loop, score=stop)

    assert len(model.calls) == 2
    assert [kind for _, kind, _ in _steps(engine)] == ["model", "command", "model"]


def test_a_lone_surrogate_in_a_command_gets_the_bound_error_and_opens_no_box(
    owner: tuple[User, int], engine: Engine, step_log: StepLogStore, trees: TreeStore, tmp_path: Path
) -> None:
    """Text arguments with an escaped lone surrogate parse to a command that cannot encode, so it never runs.

    Args:
        owner: Alice and her budget generation.
        engine: The database.
        step_log: The step log.
        trees: The tree store.
        tmp_path: Pytest's temporary folder.
    """
    text = '{"command": "echo \\ud800"}'
    bad: Turn = lambda n: {"id": "bad", "name": "bash", "arguments": text}  # noqa: E731
    model = _Researcher([bad, *_ROUND])
    loop, boxes = _loop(owner, engine, step_log, trees, tmp_path, model, generation=1)

    _run(loop)

    assert _tool_result(model, 1)["error"].startswith("Send the bash tool arguments as JSON")
    # The arguments are refused, so the transcript holds an empty object, as for every refused argument.
    assert model.calls[1][-2]["tool_calls"][0]["function"]["arguments"] == "{}"
    assert [kind for _, kind, _ in _steps(engine)][:3] == ["model", "model", "command"]
    assert boxes.opened == 2


@pytest.mark.parametrize(
    "arguments", [{"command": "echo \ud800"}, '{"command": "echo \ud800"}'], ids=["mapping", "raw-text"]
)
def test_arguments_with_a_raw_lone_surrogate_get_the_bound_error_and_an_empty_transcript(arguments: Any) -> None:
    """``_parse`` refuses such arguments, so the tool loop gives the bound error with no crash.

    Args:
        arguments: The arguments with a raw lone surrogate, as a mapping or as JSON text.
    """
    call = {"id": "bad", "name": "bash", "arguments": arguments}

    def run_command(tree: str, command: str, env: Any) -> Any:
        """Fail the test: a refused command must not run.

        Args:
            tree: Not used.
            command: Not used.
            env: Not used.

        Raises:
            AssertionError: Always.
        """
        raise AssertionError("the command ran")

    output, tree = loop_tools.run_bash(SimpleNamespace(run_command=run_command), "t", call, misuse="misuse")

    assert json.loads(output)["error"].startswith("Send the bash tool arguments as JSON")
    assert tree == "t"
    assert loop_tools.openai_call(call)["function"]["arguments"] == "{}"


def test_each_scored_case_and_version_writes_the_native_run_log_events(
    owner: tuple[User, int],
    engine: Engine,
    step_log: StepLogStore,
    trees: TreeStore,
    tmp_path: Path,
    caplog: pytest.LogCaptureFixture,
) -> None:
    """A full sweep logs ``case.scored`` for each case and ``candidate.scored`` once, as the native runner does.

    Args:
        owner: Alice and her budget generation.
        engine: The database.
        step_log: The step log.
        trees: The tree store.
        tmp_path: Pytest's temporary folder.
        caplog: Pytest's log capture.
    """
    loop, _ = _loop(owner, engine, step_log, trees, tmp_path, _Researcher(), generation=1)

    with caplog.at_level(logging.INFO, logger=ar_mod.logger.name):
        _run(loop)

    logged = [
        (record.run_log["event"], record.run_log["candidate"], record.run_log["case"], record.run_log["fields"])
        for record in caplog.records
        if record.name == ar_mod.logger.name
    ]
    assert logged == [
        ("case.scored", "0", "0", {"score": 5.0, "total": 2}),
        ("case.scored", "0", "1", {"score": 5.0, "total": 2}),
        ("candidate.scored", "0", None, {"score": 5.0, "total_evals": 2}),
        ("case.scored", "1", "0", {"score": 6.0, "total": 2}),
        ("case.scored", "1", "1", {"score": 6.0, "total": 2}),
        ("candidate.scored", "1", None, {"score": 6.0, "total_evals": 4}),
    ]


@pytest.mark.parametrize("bad", [None, math.nan])
def test_a_crashed_case_of_a_full_sweep_writes_no_case_event(
    bad: float | None,
    owner: tuple[User, int],
    engine: Engine,
    step_log: StepLogStore,
    trees: TreeStore,
    tmp_path: Path,
    caplog: pytest.LogCaptureFixture,
) -> None:
    """A case that crashes or scores NaN gets no ``case.scored`` log line or event; the other case still does.

    Args:
        bad: The crashed case's score: a metric error or NaN.
        owner: Alice and her budget generation.
        engine: The database.
        step_log: The step log.
        trees: The tree store.
        tmp_path: Pytest's temporary folder.
        caplog: Pytest's log capture.
    """

    def score(loop: EngineLoop, candidate: Any, case: Any) -> tuple[float | None, dict[str, Any]]:
        """Crash on the second case, and score the first by length.

        Args:
            loop: The engine loop.
            candidate: The candidate text.
            case: The case.

        Returns:
            The score, or ``bad`` for a crash, and no side information.
        """
        del loop
        return (bad if case == _CASES[1] else float(len(str(candidate)))), {}

    loop, _ = _loop(owner, engine, step_log, trees, tmp_path, _Researcher(), generation=1)
    events: list[tuple[str, dict[str, Any]]] = []

    with caplog.at_level(logging.INFO, logger=ar_mod.logger.name):
        _run(loop, events=events, max_evals=4, stop_at_score=None, score=score)

    logged = [
        (record.run_log["candidate"], record.run_log["case"])
        for record in caplog.records
        if record.name == ar_mod.logger.name and record.run_log["event"] == "case.scored"
    ]
    assert logged == [("0", "0"), ("1", "0")]
    scored = [(data["trial"], data["example_id"]) for kind, data in events if kind == PROGRESS_CASE_SCORED]
    assert scored == [(0, "0"), (1, "0")]


def test_a_model_call_that_fails_after_exactly_one_tool_call_ends_only_the_round(
    owner: tuple[User, int], engine: Engine, step_log: StepLogStore, trees: TreeStore, tmp_path: Path
) -> None:
    """One tool call is enough for the failure to come from tool output, so the run goes on to the next round.

    Args:
        owner: Alice and her budget generation.
        engine: The database.
        step_log: The step log.
        trees: The tree store.
        tmp_path: Pytest's temporary folder.
    """
    # The first call scores the seed file, so round 1 makes exactly one tool call before its model call fails.
    seed = {"id": "seed", "name": "evaluate", "arguments": {"file": "work/seed.txt"}}
    model = _Researcher([lambda n: seed, *_ROUND], fail_at=frozenset({1, 2, 3}))
    loop, _ = _loop(owner, engine, step_log, trees, tmp_path, model, generation=1)

    result = _run(loop)

    assert (result.best_candidate, result.best_score) == ("seedxx", 6.0)
    assert result.metadata["rounds"] == 2
    round_one = ["model", *["model"] * loop_tools.MODEL_ATTEMPTS]
    assert [kind for _, kind, _ in _steps(engine)] == [*round_one, "model", "model", "command", "model", "model"]


def test_an_executable_in_work_stays_executable_in_the_next_round_and_a_plain_file_stays_plain(
    owner: tuple[User, int], engine: Engine, step_log: StepLogStore, trees: TreeStore, tmp_path: Path
) -> None:
    """Round 1 makes ``work/check.sh`` executable; round 2 runs it, and ``work/plain.sh`` is still not executable.

    Args:
        owner: Alice and her budget generation.
        engine: The database.
        step_log: The step log.
        trees: The tree store.
        tmp_path: Pytest's temporary folder.
    """
    make = (
        "printf '#!/bin/sh\\necho ran\\n' > work/check.sh && cp work/check.sh work/plain.sh && chmod +x work/check.sh"
    )
    check = "./work/check.sh && test ! -x work/plain.sh && echo plain"
    script = [_bash(f"if [ {{n}} = 1 ]; then {make}; else {check}; fi"), *_ROUND]
    model = _Researcher(script)
    loop, _ = _loop(owner, engine, step_log, trees, tmp_path, model, generation=1)

    result = _run(loop)

    assert result.metadata["rounds"] == 2
    # Round 1 makes three calls and a final reply; round 2's second call gets its first command's result.
    output = _tool_result(model, 5)
    assert (output["exit_code"], output["stdout"]) == (0, "ran\nplain\n")
    lab = _lab(tmp_path)
    assert os.access(lab / "work" / "check.sh", os.X_OK)
    assert not os.access(lab / "work" / "plain.sh", os.X_OK)


def test_the_result_names_the_upstream_source_and_revision(
    owner: tuple[User, int], engine: Engine, step_log: StepLogStore, trees: TreeStore, tmp_path: Path
) -> None:
    """The result metadata names the AutoResearch source and version, as the native runtime's result does.

    Args:
        owner: Alice and her budget generation.
        engine: The database.
        step_log: The step log.
        trees: The tree store.
        tmp_path: Pytest's temporary folder.
    """
    loop, _ = _loop(owner, engine, step_log, trees, tmp_path, _Researcher(), generation=1)

    result = _run(loop)

    assert result.metadata["upstream_source"] == f"skynet:autoresearch@{native_engines.AUTORESEARCH_VERSION}"
    assert result.metadata["upstream_revision"] == native_engines.AUTORESEARCH_VERSION


def test_an_unknown_tool_with_over_long_arguments_gets_the_misuse_error() -> None:
    """The tool name is checked before the bounds, so an unknown tool never gets the bash bound error."""
    call = {"id": "x", "name": "edit", "arguments": "x" * (loop_tools.MAX_ARGUMENT_CHARS + 1)}

    def run_command(tree: str, command: str, env: Any) -> Any:
        """Fail the test: a refused call must not run.

        Args:
            tree: Not used.
            command: Not used.
            env: Not used.

        Raises:
            AssertionError: Always.
        """
        raise AssertionError("the command ran")

    output, tree = loop_tools.run_bash(SimpleNamespace(run_command=run_command), "t", call, misuse="misuse")

    assert json.loads(output) == {"error": "misuse"}
    assert tree == "t"


def test_text_arguments_past_the_depth_bound_get_an_empty_transcript() -> None:
    """Text arguments get the same depth bound as mapping arguments in the transcript; at the bound they stay."""
    at_bound = {"pad": functools.reduce(lambda inner, _: [inner], range(loop_tools.MAX_ARGUMENT_DEPTH - 1), 0)}
    past_bound = json.dumps({"pad": [at_bound["pad"]]})
    past_parser = "[" * 100_000 + "]" * 100_000

    def sent(arguments: Any) -> Any:
        """Return the transcript arguments of a bash call.

        Args:
            arguments: The call's arguments.

        Returns:
            The arguments as the transcript holds them.
        """
        return loop_tools.openai_call({"id": "c", "name": "bash", "arguments": arguments})["function"]["arguments"]

    assert sent(json.dumps(at_bound)) == json.dumps(at_bound)
    assert sent(past_bound) == "{}"
    assert sent(past_parser) == "{}"
    assert sent({"pad": [at_bound["pad"]]}) == "{}"
    assert sent("not json") == "not json"


def test_the_depth_bound_on_text_does_not_depend_on_the_stack() -> None:
    """Deep text gives the same transcript and error from a deep stack as from the top.

    Brackets inside JSON strings do not count. A live reply and its replay run at different stack depths, so a
    stack-dependent result would change the prompt.
    """
    at_bound = "[" * loop_tools.MAX_ARGUMENT_DEPTH + "]" * loop_tools.MAX_ARGUMENT_DEPTH
    in_string = json.dumps({"command": "echo " + '[{\\"' * 1000})
    # Many shallow lists side by side: the depth goes down at each close, so the text stays inside the bound.
    wide = "[" + "[]," * 40 + "[]]"
    # A closing brace also takes the depth down, so many shallow objects side by side stay inside the bound.
    wide_objects = "[" + "{}," * 40 + "{}]"
    # The escape ends after one character, so the string closes and the deep brackets after it count. The text is
    # not JSON, so only the text count, not the parsed-value check, can refuse it.
    after_escape = '["a\\nb", ' + "[" * loop_tools.MAX_ARGUMENT_DEPTH
    texts = [
        "[" * 990,
        "[" * 900 + "]" * 900,
        "[" * (loop_tools.MAX_ARGUMENT_DEPTH + 1),
        at_bound,
        in_string,
        wide,
        wide_objects,
        after_escape,
    ]

    def results() -> list[tuple[Any, bool]]:
        """Give each text's transcript arguments and whether the tool loop refuses it.

        Returns:
            One ``(transcript arguments, refused)`` pair for each text.
        """
        calls = [{"id": "c", "name": "bash", "arguments": text} for text in texts]
        return [
            (
                loop_tools.openai_call(call)["function"]["arguments"],
                loop_tools.refused_arguments(call, "bash") is not None,
            )
            for call in calls
        ]

    def deep(levels: int) -> list[tuple[Any, bool]]:
        """Give ``results()`` from ``levels`` frames further down the stack.

        Args:
            levels: The number of frames to add.

        Returns:
            The results at that depth.
        """
        return results() if levels == 0 else deep(levels - 1)

    top = results()
    assert top == [
        ("{}", True),
        ("{}", True),
        ("{}", True),
        (at_bound, False),
        (in_string, False),
        (wide, False),
        (wide_objects, False),
        ("{}", True),
    ]
    assert deep(sys.getrecursionlimit() - 200) == top


def test_compact_text_that_grows_past_the_bound_gives_an_empty_transcript_object() -> None:
    """Compact JSON text inside the length bound grows past it when dumped with spaces, so the tool loop refuses it.

    The transcript must then hold ``{}``, not the refused text, as for every other refused argument.
    """
    compact = json.dumps({f"k{i}": i for i in range(14_000)}, separators=(",", ":"))
    assert len(compact) <= loop_tools.MAX_ARGUMENT_CHARS
    assert len(json.dumps(json.loads(compact), ensure_ascii=False)) > loop_tools.MAX_ARGUMENT_CHARS
    call = {"id": "c", "name": "bash", "arguments": compact}

    assert loop_tools.refused_arguments(call, "bash") is not None
    assert loop_tools.openai_call(call)["function"]["arguments"] == "{}"


def test_text_that_is_not_json_with_a_raw_lone_surrogate_gives_an_empty_transcript_object() -> None:
    """Text that is not JSON passes the bound and gets the misuse error, but its lone surrogate stays out of the prompt."""
    call = {"id": "c", "name": "bash", "arguments": '{"command": "echo \ud800'}

    def run_command(tree: str, command: str, env: Any) -> Any:
        """Fail the test: a call with no command must not run.

        Args:
            tree: Not used.
            command: Not used.
            env: Not used.

        Raises:
            AssertionError: Always.
        """
        raise AssertionError("the command ran")

    assert loop_tools.refused_arguments(call, "bash") is None
    output, tree = loop_tools.run_bash(SimpleNamespace(run_command=run_command), "t", call, misuse="misuse")

    assert json.loads(output) == {"error": "misuse"}
    assert tree == "t"
    assert loop_tools.openai_call(call)["function"]["arguments"] == "{}"


def test_an_evaluation_of_the_lab_parent_is_refused(
    owner: tuple[User, int], engine: Engine, step_log: StepLogStore, trees: TreeStore, tmp_path: Path
) -> None:
    """``file=".."`` names the folder above the lab, so the evaluate tool refuses it and spends nothing.

    Args:
        owner: Alice and her budget generation.
        engine: The database.
        step_log: The step log.
        trees: The tree store.
        tmp_path: Pytest's temporary folder.
    """
    parent: Turn = lambda n: {"id": "p", "name": "evaluate", "arguments": {"file": ".."}}  # noqa: E731
    model = _Researcher([parent])
    loop, boxes = _loop(owner, engine, step_log, trees, tmp_path, model, generation=1)

    with pytest.raises(RuntimeError, match="without scoring any candidate through the evaluate tool"):
        _run(loop)

    assert boxes.opened == 0
    assert _tool_result(model, 1) == {"error": "Give `file` as a path inside the lab."}


def test_a_lab_removal_that_fails_leaves_no_version_and_ends_the_run(
    owner: tuple[User, int], engine: Engine, step_log: StepLogStore, trees: TreeStore, tmp_path: Path
) -> None:
    """The ``rm`` of the lab exits with code 1, so the round leaves no version: nothing is swept (SKY-825).

    Args:
        owner: Alice and her budget generation.
        engine: The database.
        step_log: The step log.
        trees: The tree store.
        tmp_path: Pytest's temporary folder.
    """
    model = _Researcher([_bash("printf %s {x} >> a.py")])
    loop, _ = _loop(owner, engine, step_log, trees, tmp_path, model, generation=1)
    run = loop.run

    def fail_removal(tree_id: str, command: str, grants: tuple[Any, ...]) -> Any:
        """Run the command, and give the lab removal exit code 1 with the tree it left.

        Args:
            tree_id: The tree to run in.
            command: The shell command.
            grants: The command's grants.

        Returns:
            The command result and the tree it left.
        """
        result, tree = run(tree_id, command, grants)
        if command.startswith("rm -rf "):
            result = dataclasses.replace(result, exit_code=1)
        return result, tree

    loop.run = fail_removal  # type: ignore[method-assign]
    seed = _tree(trees, "a.py", "a")

    result = _run(loop, score=_score_a_py, repo_seed=RepoSeed(tree_id=seed))

    assert (result.best_candidate, result.total_evals, result.metadata["rounds"]) == (seed, 2, 1)


def test_a_lab_removal_that_times_out_leaves_no_version_and_ends_the_run(
    owner: tuple[User, int], engine: Engine, step_log: StepLogStore, trees: TreeStore, tmp_path: Path
) -> None:
    """The ``rm`` of the lab times out with exit code 0, so the round leaves no version: nothing is swept (SKY-825).

    Args:
        owner: Alice and her budget generation.
        engine: The database.
        step_log: The step log.
        trees: The tree store.
        tmp_path: Pytest's temporary folder.
    """
    model = _Researcher([_bash("printf %s {x} >> a.py")])
    loop, _ = _loop(owner, engine, step_log, trees, tmp_path, model, generation=1)
    run = loop.run

    def time_out_removal(tree_id: str, command: str, grants: tuple[Any, ...]) -> Any:
        """Run the command, and mark the lab removal as timed out with exit code 0 and the tree it left.

        Args:
            tree_id: The tree to run in.
            command: The shell command.
            grants: The command's grants.

        Returns:
            The command result and the tree it left.
        """
        result, tree = run(tree_id, command, grants)
        if command.startswith("rm -rf "):
            result = dataclasses.replace(result, exit_code=0, timed_out=True)
        return result, tree

    loop.run = time_out_removal  # type: ignore[method-assign]
    seed = _tree(trees, "a.py", "a")

    result = _run(loop, score=_score_a_py, repo_seed=RepoSeed(tree_id=seed))

    assert (result.best_candidate, result.total_evals, result.metadata["rounds"]) == (seed, 2, 1)


def test_a_lab_path_check_that_times_out_fails_without_blaming_the_repository(
    owner: tuple[User, int], engine: Engine, step_log: StepLogStore, trees: TreeStore, tmp_path: Path
) -> None:
    """The seed's lab-path check times out with exit code 1, so the error names the timeout, not a repository file.

    Args:
        owner: Alice and her budget generation.
        engine: The database.
        step_log: The step log.
        trees: The tree store.
        tmp_path: Pytest's temporary folder.
    """
    model = _Researcher()
    loop, _ = _loop(owner, engine, step_log, trees, tmp_path, model, generation=1)
    run = loop.run

    def time_out_check(tree_id: str, command: str, grants: tuple[Any, ...]) -> Any:
        """Run the command, and mark the lab-path check as timed out with exit code 1.

        Args:
            tree_id: The tree to run in.
            command: The shell command.
            grants: The command's grants.

        Returns:
            The command result and the tree it left.
        """
        result, tree = run(tree_id, command, grants)
        if command.startswith("test ! -e "):
            result = dataclasses.replace(result, exit_code=1, timed_out=True)
        return result, tree

    loop.run = time_out_check  # type: ignore[method-assign]

    with pytest.raises(ServiceError, match="the check timed out"):
        _run(loop, repo_seed=RepoSeed(tree_id=_tree(trees, "a.py", "a")))
    assert model.calls == []


def test_a_repository_session_with_no_tool_call_opens_no_box_to_remove_the_lab(
    owner: tuple[User, int], engine: Engine, step_log: StepLogStore, trees: TreeStore, tmp_path: Path
) -> None:
    """A session that runs no command leaves the parent's tree, so no ``rm`` box opens (SKY-825).

    Args:
        owner: Alice and her budget generation.
        engine: The database.
        step_log: The step log.
        trees: The tree store.
        tmp_path: Pytest's temporary folder.
    """
    model = _Researcher([])
    loop, boxes = _loop(owner, engine, step_log, trees, tmp_path, model, generation=1)
    run = loop.run
    commands: list[str] = []

    def recorded(tree_id: str, command: str, grants: tuple[Any, ...]) -> Any:
        """Record each command, then run it.

        Args:
            tree_id: The tree to run in.
            command: The shell command.
            grants: The command's grants.

        Returns:
            The command result and the tree it left.
        """
        commands.append(command)
        return run(tree_id, command, grants)

    loop.run = recorded  # type: ignore[method-assign]
    seed = _tree(trees, "a.py", "a")

    result = _run(loop, score=_score_a_py, repo_seed=RepoSeed(tree_id=seed))

    assert (result.best_candidate, result.total_evals, result.metadata["rounds"]) == (seed, 2, 1)
    # The only command checks that the seed has no lab folder of its own.
    assert commands == [f"test ! -e {ar_mod._REPO_LAB} && test ! -L {ar_mod._REPO_LAB}"]
    assert boxes.opened == 1


@pytest.mark.parametrize("repo", [False, True])
def test_the_state_budget_line_of_a_repository_run_says_the_engine_gives_one_full_evaluation(
    repo: bool, tmp_path: Path
) -> None:
    """A repository session cannot run the evaluator, so ``STATE.md`` names no round quota; a text run keeps it.

    Args:
        repo: Whether the run is a repository run.
        tmp_path: Pytest's temporary folder.
    """
    config = OptimizeAnythingConfig(engine="autoresearch", run_dir=str(tmp_path), engine_config={"model": ""})
    ctx = EngineContext(reflection_lm=str, run_dir=str(tmp_path), repo_seed=RepoSeed(tree_id="t") if repo else None)
    worker = ar_mod._WorkerAutoResearch(config, ctx, max_tool_calls=100)
    worker.example_ids = ["0", "1"]
    (worker.work_dir / "frontier").mkdir(parents=True)

    worker._write_state(SimpleNamespace(budget=SimpleNamespace(remaining=6)), "survey")

    budget = (worker.work_dir / "STATE.md").read_text(encoding="utf-8").split("## Budget\n\n")[1].split("\n")[0]
    if repo:
        assert budget == "- The engine gives this round's version one full evaluation, of the 6 evaluation units left."
    else:
        assert budget.startswith("- This round may spend ")
        assert budget.endswith(" of the 6 left.")


def test_a_repository_run_never_gets_the_combine_directive(
    owner: tuple[User, int], engine: Engine, step_log: StepLogStore, trees: TreeStore, tmp_path: Path
) -> None:
    """Round one's version beats the seed on one case and loses on the other, so the frontier holds two versions.

    A text run would combine them in round two; a repository session sees only the leader's tree (SKY-825).

    Args:
        owner: Alice and her budget generation.
        engine: The database.
        step_log: The step log.
        trees: The tree store.
        tmp_path: Pytest's temporary folder.
    """

    def score(loop: EngineLoop, candidate: Any, case: Any) -> tuple[float | None, dict[str, Any]]:
        """Score case ``a`` by the length of ``a.py``, and case ``b`` by ten less half that length.

        Args:
            loop: The engine loop.
            candidate: The version's tree id.
            case: The case.

        Returns:
            The score and no side information.
        """
        length, info = _score_a_py(loop, candidate, case)
        return (length if case == _CASES[0] else 10.0 - length / 2), info

    model = _Researcher([_bash("printf x >> a.py")])
    loop, _ = _loop(owner, engine, step_log, trees, tmp_path, model, generation=1)

    result = _run(loop, stop_at_score=5.7, score=score, repo_seed=RepoSeed(tree_id=_tree(trees, "a.py", "a")))

    assert loop.read_file(result.best_candidate, "a.py") == b"axx"
    assert "combine" not in result.metadata["directives"]
    assert result.metadata["directives"] == ["survey", "exploit"]


def test_a_text_metric_error_with_no_cases_shows_as_a_candidate_with_no_score(
    owner: tuple[User, int], engine: Engine, step_log: StepLogStore, trees: TreeStore, tmp_path: Path
) -> None:
    """With no cases, round one's file is a metric error: it shows, with no score, as a repository version does.

    Args:
        owner: Alice and her budget generation.
        engine: The database.
        step_log: The step log.
        trees: The tree store.
        tmp_path: Pytest's temporary folder.
    """

    def score(loop: EngineLoop, candidate: Any, case: Any) -> tuple[float | None, dict[str, Any]]:
        """Give round one's file a metric error, and score any other by its length.

        Args:
            loop: The engine loop.
            candidate: The candidate text.
            case: Not used.

        Returns:
            The score, or ``None`` for the metric error, and no side information.
        """
        del loop, case
        return (None if candidate == "seedx" else float(len(str(candidate)))), {}

    loop, _ = _loop(owner, engine, step_log, trees, tmp_path, _Researcher(), generation=1)
    events: list[tuple[str, dict[str, Any]]] = []

    result = _run(loop, events=events, cases=[], score=score)

    assert [data["score"] for kind, data in events if kind == PROGRESS_CANDIDATE] == [None, 6.0]
    assert (result.best_candidate, result.best_score) == ("seedxx", 6.0)
