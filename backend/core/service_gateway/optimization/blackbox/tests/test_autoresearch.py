"""Tests for AutoResearch in the worker engine loop (SKY-725).

The tests use the real engine loop over a SQLite file, local subprocess boxes,
a scripted fake model and a pure Python scorer: no network and no paid call.
"""

from __future__ import annotations

import errno
import functools
import json
import math
import re
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
from core.constants import PROGRESS_CANDIDATE, PROGRESS_CASE_SCORED
from core.exceptions import ServiceError
from core.models.blackbox import BLACKBOX_HARNESS_CUSTOM, BLACKBOX_HARNESS_PI, BlackboxProposer
from core.service_gateway.optimization.blackbox import autoresearch as ar_mod
from core.service_gateway.optimization.blackbox import loop_tools, native_engines
from core.service_gateway.optimization.blackbox.autoresearch import BASH_TOOL, EVALUATE_TOOL, AutoResearchEngine
from core.service_gateway.optimization.blackbox.native_engines import ProposerFailedError
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
from core.worker.engine_loop import EngineLoop, ModelCallBilledError, ModelCallFailedError
from core.worker.tests import test_engine_loop as loop_tests
from core.worker.tests.test_engine_loop import (
    RUN_ID,
    _Boxes,
    _CountingSource,
    _new_generation,
    _steps,
    _tree,
    _WorkerDiedError,
)

# The engine-loop fixtures: a SQLite file, its step log and tree store, and alice with a run budget.
engine = loop_tests.engine
step_log = loop_tests.step_log
trees = loop_tests.trees
owner = loop_tests.owner

_CASES = [{"id": "a"}, {"id": "b"}]
# A scripted turn: the tool call to make in a round, or ``None`` for a final reply.
Turn = Callable[[int], dict[str, Any] | None]
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
            The scripted tool call of this turn, or a final reply.

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
        return ModelReply("done" if call is None else "", self.cost, [] if call is None else [call])


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


def test_the_no_evaluation_watchdog_counts_model_turns_since_the_budget_last_changed(
    owner: tuple[User, int], engine: Engine, step_log: StepLogStore, trees: TreeStore, tmp_path: Path
) -> None:
    """Sixty seconds is two turns: each round gets two turns after its sweep, then stops, and the run goes on.

    Args:
        owner: Alice and her budget generation.
        engine: The database.
        step_log: The step log.
        trees: The tree store.
        tmp_path: Pytest's temporary folder.
    """
    model = _Researcher(rest=_bash("true"))
    loop, _ = _loop(owner, engine, step_log, trees, tmp_path, model, generation=1)

    result = _run(loop, native_options=_options(max_no_eval_seconds=60))

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


def test_a_repository_run_is_refused(
    owner: tuple[User, int], engine: Engine, step_log: StepLogStore, trees: TreeStore, tmp_path: Path
) -> None:
    """A repository run needs ``checkout.sh`` and git in the box, which this port does not give yet.

    Args:
        owner: Alice and her budget generation.
        engine: The database.
        step_log: The step log.
        trees: The tree store.
        tmp_path: Pytest's temporary folder.
    """
    model = _Researcher()
    loop, _ = _loop(owner, engine, step_log, trees, tmp_path, model, generation=1)

    with pytest.raises(ServiceError, match="repository"):
        _run(loop, repo_seed=RepoSeed(tree_id=_tree(trees, "a.py", "a")))
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
    """A command output past ``OUTPUT_CHARS`` keeps its head and tail, so the next prompt stays bounded.

    Args:
        owner: Alice and her budget generation.
        engine: The database.
        step_log: The step log.
        trees: The tree store.
        tmp_path: Pytest's temporary folder.
    """
    big = f"python3 -c \"print('a' * {loop_tools.OUTPUT_CHARS + 50})\""
    model = _Researcher([_bash(big), *_ROUND])
    loop, _ = _loop(owner, engine, step_log, trees, tmp_path, model, generation=1)

    _run(loop)

    stdout = _tool_result(model, 1)["stdout"]
    assert len(stdout) < loop_tools.OUTPUT_CHARS + 50
    assert "[... 51 characters cut ...]" in stdout


def test_a_long_evaluate_result_is_cut(
    owner: tuple[User, int], engine: Engine, step_log: StepLogStore, trees: TreeStore, tmp_path: Path
) -> None:
    """An evaluate result past ``OUTPUT_CHARS`` keeps its head and tail, so the next prompt stays bounded.

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
        return float(len(str(candidate))), {"log": "z" * loop_tools.OUTPUT_CHARS}

    model = _Researcher()
    loop, _ = _loop(owner, engine, step_log, trees, tmp_path, model, generation=1)

    _run(loop, max_evals=2, stop_at_score=None, score=score)

    result = model.calls[2][-1]
    assert result["role"] == "tool"
    assert len(result["content"]) < loop_tools.OUTPUT_CHARS + 100
    assert "characters cut ...]" in result["content"]


def test_a_prompt_past_its_cap_stops_the_session_and_the_run_goes_on(
    owner: tuple[User, int],
    engine: Engine,
    step_log: StepLogStore,
    trees: TreeStore,
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """A round whose transcript passes ``MAX_PROMPT_CHARS`` asks the model no more; the next round still runs.

    Args:
        owner: Alice and her budget generation.
        engine: The database.
        step_log: The step log.
        trees: The tree store.
        tmp_path: Pytest's temporary folder.
        monkeypatch: Pytest's monkeypatch.
    """
    monkeypatch.setattr(ar_mod, "MAX_PROMPT_CHARS", 3_000)
    model = _Researcher([*_ROUND, _bash("python3 -c \"print('y' * 3000)\"")], rest=_bash("true"))
    loop, _ = _loop(owner, engine, step_log, trees, tmp_path, model, generation=1)

    result = _run(loop)

    assert (result.best_candidate, result.best_score) == ("seedxx", 6.0)
    assert result.metadata["rounds"] == 2
    assert len(model.calls) == 6
    assert [kind for _, kind, _ in _steps(engine)] == ["model", "command", "model", "model", "command"] * 2


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

    A NaN or infinite case score is a crash, as a metric error is (SKY-751).

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
            The score, or ``bad`` for a crash, and no side information.
        """
        del loop
        return (bad if case == _CASES[1] else float(len(str(candidate)))), {}

    loop, _ = _loop(owner, engine, step_log, trees, tmp_path, _Researcher(), generation=1)
    events: list[tuple[str, dict[str, Any]]] = []

    result = _run(loop, events=events, max_evals=4, stop_at_score=None, score=score)

    assert result.best_candidate == "seedxx"
    assert result.best_score is None
    assert [(data["score"], data["per_example"]) for kind, data in events if kind == PROGRESS_CANDIDATE] == [
        (None, []),
        (None, []),
    ]


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


@pytest.mark.parametrize(
    "arguments",
    [
        '{"file": ' + "[" * (loop_tools.MAX_ARGUMENT_DEPTH + 1) + "]" * (loop_tools.MAX_ARGUMENT_DEPTH + 1) + "}",
        {
            "file": "work/c1.txt",
            "pad": functools.reduce(lambda inner, _: [inner], range(loop_tools.MAX_ARGUMENT_DEPTH), 0),
        },
        {"file": "work/c1.txt", "pad": "x" * loop_tools.MAX_ARGUMENT_CHARS},
    ],
    ids=["deep-text", "deep-mapping", "long-mapping"],
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
    assert transcript == (arguments if isinstance(arguments, str) else "{}")
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
