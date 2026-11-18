"""Tests for AutoResearch in the worker engine loop (SKY-725).

The tests use the real engine loop over a SQLite file, local subprocess boxes,
a scripted fake model and a pure Python scorer: no network and no paid call.
"""

from __future__ import annotations

import json
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

from core.constants import PROGRESS_CANDIDATE, PROGRESS_CASE_SCORED
from core.exceptions import ServiceError
from core.service_gateway.optimization.blackbox import autoresearch as ar_mod
from core.service_gateway.optimization.blackbox import native_engines
from core.service_gateway.optimization.blackbox.autoresearch import BASH_TOOL, EVALUATE_TOOL, AutoResearchEngine
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
        self, script: list[Turn] | None = None, *, die_at: int | None = None, cost: str = "1", rest: Turn | None = None
    ) -> None:
        """Set the script.

        Args:
            script: The turns of each round; one write and one full evaluation when ``None``.
            die_at: The index of the call in which the worker dies; ``None`` never dies.
            cost: The cost of each reply, in cents.
            rest: The turn after the script ends; a final reply when ``None``.
        """
        self.script = _ROUND if script is None else script
        self.die_at = die_at
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
        """
        if len(self.calls) == self.die_at:
            raise _WorkerDiedError
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
    **fields: Any,
) -> Result:
    """Run AutoResearch on the loop; with the default script it stops at the second round's score.

    Args:
        loop: The engine loop.
        events: A list that collects the progress events.
        cases: The task's cases; two cases when ``None``.
        max_evals: The scorer calls of the run.
        stop_at_score: The score that ends the run.
        **fields: Other context fields.

    Returns:
        The engine's result.
    """
    sink = None if events is None else (lambda kind, data: events.append((kind, data)))
    ctx = loop.context(stop_at_score=stop_at_score, progress_callback=sink, **fields)
    task = Task("seed", cases=list(_CASES if cases is None else cases))
    return AutoResearchEngine().run(task, loop.server(_score, max_evals=max_evals), ctx)


def _options(
    *, max_token_cost: float | None = None, max_tool_calls: int = 100, max_no_eval_seconds: float | None = None
) -> SimpleNamespace:
    """Build the native options that the engine reads.

    Args:
        max_token_cost: The proposer spend cap in dollars, or ``None``.
        max_tool_calls: The tool calls of one session.
        max_no_eval_seconds: The no-evaluation watchdog, or ``None``.

    Returns:
        The options.
    """
    proposer = SimpleNamespace(ralph=True, max_tool_calls=max_tool_calls, max_no_eval_seconds=max_no_eval_seconds)
    return SimpleNamespace(model="m", max_token_cost=max_token_cost, proposer=proposer)


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
    """With the worker host, the engine runs the native loop in the worker, not through the box runtime."""
    native = MagicMock(side_effect=AssertionError("the native runtime ran"))
    monkeypatch.setattr(ar_mod, "run_native_engine", native)
    loop, _ = _loop(owner, engine, step_log, trees, tmp_path, _Researcher(), generation=1)

    assert _run(loop).best_candidate == "seedxx"
    native.assert_not_called()


def test_a_context_without_the_worker_host_runs_the_native_runtime(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Until the SKY-728 cutover, a run outside the worker engine loop keeps the native box runtime."""
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
    """Each round is a reply with a command, a reply with an evaluation and a final reply; scoring opens no box."""
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
    """Kill the worker in round two's second model call, then resume: no committed reply or command runs again."""
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
    """The lab is a new tree each round, so the agent's files must come back from the tree after a round."""
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
    """A probe scores and spends budget but streams no case; each full sweep streams its cases and one candidate."""
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
    """With one scorer call left for two cases, round two's sweep gets the marker; the round scored nothing."""
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
    """Round one's last sweep is refused with one unit left; round two starts and spends it on a probe."""
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
    """With no cases, the evaluate tool scores the file once and logs it as a checkpoint."""
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
    """An agent that keeps working after it spends its round's evaluations gets a fixed number of model turns."""
    model = _Researcher(rest=_bash("true"))
    loop, _ = _loop(owner, engine, step_log, trees, tmp_path, model, generation=1)

    result = _run(loop, max_evals=2, stop_at_score=None)

    assert result.best_candidate == "seedx"
    assert len(model.calls) == 2 + ar_mod._WRITE_UP_TURNS
    assert result.metadata["rounds"] == 1


def test_a_bad_tool_call_gets_an_error_result_and_no_step(
    owner: tuple[User, int], engine: Engine, step_log: StepLogStore, trees: TreeStore, tmp_path: Path
) -> None:
    """A call to an unknown tool, or an evaluation of a path outside the lab, goes back to the model as an error."""
    edit: Turn = lambda n: {"id": "x", "name": "edit", "arguments": "{}"}  # noqa: E731
    model = _Researcher(
        [edit, _evaluate(), lambda n: {"id": "y", "name": "evaluate", "arguments": {"file": "a/../../x"}}]
    )
    loop, boxes = _loop(owner, engine, step_log, trees, tmp_path, model, generation=1)

    with pytest.raises(RuntimeError, match="without scoring any candidate"):
        _run(loop)

    assert boxes.opened == 0
    assert [kind for _, kind, _ in _steps(engine)] == ["model"] * 4
    assert "error" in _tool_result(model, 1)
    assert "No file work/c1.txt" in _tool_result(model, 2)["error"]
    assert "inside the lab" in _tool_result(model, 3)["error"]


def test_a_lab_file_whose_name_starts_with_two_dots_is_scored(
    owner: tuple[User, int], engine: Engine, step_log: StepLogStore, trees: TreeStore, tmp_path: Path
) -> None:
    """Only a path that leaves the lab is refused; ``..notes.txt`` is a file inside it."""
    notes: Turn = lambda n: {"id": "z", "name": "evaluate", "arguments": {"file": "..notes.txt"}}  # noqa: E731
    model = _Researcher([_bash("printf %s seedx > ..notes.txt"), notes])
    loop, _ = _loop(owner, engine, step_log, trees, tmp_path, model, generation=1)

    result = _run(loop, max_evals=2)

    assert _tool_result(model, 2)["average_score"] == 5.0
    assert (result.best_candidate, result.best_score) == ("seedx", 5.0)


def test_a_session_that_only_runs_commands_stops_at_the_tool_call_cap(
    owner: tuple[User, int], engine: Engine, step_log: StepLogStore, trees: TreeStore, tmp_path: Path
) -> None:
    """With no cost cap, an agent that never evaluates stops before its fourth tool call; the command never runs."""
    model = _Researcher([], rest=_bash("true"))
    loop, boxes = _loop(owner, engine, step_log, trees, tmp_path, model, generation=1)

    with pytest.raises(RuntimeError, match="without scoring any candidate"):
        _run(loop, native_options=_options(max_tool_calls=3))

    assert len(model.calls) == 4
    assert boxes.opened == 3
    assert [kind for _, kind, _ in _steps(engine)] == ["model", "command"] * 3 + ["model"]


def test_the_no_evaluation_watchdog_counts_model_turns_since_the_budget_last_changed(
    owner: tuple[User, int], engine: Engine, step_log: StepLogStore, trees: TreeStore, tmp_path: Path
) -> None:
    """Sixty seconds is two turns: each round gets two turns after its sweep, then stops, and the run goes on."""
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
    """With a cap of two cents, the round runs its two replies' tools and then asks the model no more."""
    model = _Researcher(cost="1")
    loop, boxes = _loop(owner, engine, step_log, trees, tmp_path, model, generation=1)
    result = _run(loop, native_options=_options(max_token_cost=0.02))

    assert result.best_candidate == "seedx"
    assert boxes.opened == 1
    assert [kind for _, kind, _ in _steps(engine)] == ["model", "command", "model"]
    assert result.metadata["rounds"] == 1


def test_a_repository_run_is_refused(
    owner: tuple[User, int], engine: Engine, step_log: StepLogStore, trees: TreeStore, tmp_path: Path
) -> None:
    """A repository run needs ``checkout.sh`` and git in the box, which this port does not give yet."""
    model = _Researcher()
    loop, _ = _loop(owner, engine, step_log, trees, tmp_path, model, generation=1)

    with pytest.raises(ServiceError, match="repository"):
        _run(loop, repo_seed=RepoSeed(tree_id=_tree(trees, "a.py", "a")))
    assert model.calls == []


def test_the_worker_brief_names_the_evaluate_tool_and_not_eval_sh(tmp_path: Path) -> None:
    """The lab has no ``eval.sh``, so the brief tells the agent to use the ``evaluate`` tool."""
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
