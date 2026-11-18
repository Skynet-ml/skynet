"""Tests for the ShinkaEvolve engine on the worker engine loop (SKY-724).

A small fake ``shinka`` package stands in for upstream: it scores the
starting program, then asks the model for one new version each generation,
through the same module names the real package uses. The runner child runs
in a real process; the loop uses local boxes and a fake model, so the tests
make no network and no paid call.
"""

from __future__ import annotations

import io
import stat
import sys
import textwrap
from pathlib import Path
from types import SimpleNamespace

import pytest
from sqlalchemy.engine import Engine

from core.billing.usage_tags import current_tags
from core.exceptions import ServiceError
from core.service_gateway.optimization.blackbox import sandbox_log, shinka_evolve
from core.service_gateway.optimization.blackbox.protocol import EngineContext, EvalServer, Result, Task
from core.service_gateway.optimization.blackbox.shinka_evolve import ShinkaEvolveEngine
from core.service_gateway.optimization.budget_stop import BudgetReached
from core.storage.step_log_store import StepLogStore
from core.storage.tree_store import TreeStore
from core.worker.actors import User
from core.worker.tests import test_engine_loop as loop_tests
from core.worker.tests.test_engine_loop import (
    _KINDS,
    _loop,
    _new_generation,
    _scorer,
    _steps,
    _tree,
    _WorkerDiedError,
)

# The engine-loop tests' fixtures: a SQLite run, its step log, its trees and its owner.
engine = loop_tests.engine
owner = loop_tests.owner
step_log = loop_tests.step_log
trees = loop_tests.trees

_FAKE_UPSTREAM = {
    "shinka/__init__.py": "",
    "shinka/core/__init__.py": "from .runner import EvolutionConfig, ShinkaEvolveRunner\n",
    "shinka/core/async_runner.py": "",
    "shinka/embed/__init__.py": "",
    "shinka/embed/embedding.py": "",
    "shinka/llm/__init__.py": "",
    "shinka/llm/query.py": "",
    "shinka/llm/llm.py": """
        def query(*args, **kwargs):
            raise AssertionError("The runner must replace upstream's model call.")


        async def query_async(*args, **kwargs):
            raise AssertionError("The runner must replace upstream's model call.")
    """,
    "shinka/llm/providers.py": """
        class QueryResult:
            def __init__(self, **fields):
                self.__dict__.update(fields)
    """,
    "shinka/database.py": """
        class DatabaseConfig:
            def __init__(self, **fields):
                self.__dict__.update(fields)
    """,
    "shinka/edit.py": "def summarize_diff(*args, **kwargs):\n    return ''\n",
    "shinka/launch.py": """
        class LocalJobConfig:
            def __init__(self, python_executable):
                self.python_executable = python_executable
    """,
    "shinka/local_openai_config.py": "def parse_local_openai_model(name):\n    return None\n",
    "shinka/prompts.py": "FULL_SYS_FORMATS = {}\n",
    "shinka/core/runner.py": '''
        import asyncio
        import json
        import sqlite3
        import subprocess
        import uuid
        from pathlib import Path

        from shinka.llm import llm as shinka_llm

        START = "<!-- EVOLVE-BLOCK-START -->\\n"
        END = "\\n<!-- EVOLVE-BLOCK-END -->"


        class EvolutionConfig:
            def __init__(self, **fields):
                self.__dict__.update(fields)


        class ShinkaEvolveRunner:
            """Score the start, then one model-written version per generation, one at a time."""

            def __init__(self, evo_config, job_config, db_config, init_program_str, evaluate_str, **runner):
                self.evo = evo_config
                self.python = job_config.python_executable
                self.db_path = db_config.db_path
                self.init_program = init_program_str
                self.evaluate_str = evaluate_str
                self.results_dir = Path(evo_config.results_dir)
                self.running_jobs = []

            def _get_completed_job_work_count(self):
                return 0

            def _has_background_side_effect_work(self):
                return False

            async def _generate_proposal_async(self, generation, task_id):
                text = self.best[1].split(START)[1].split(END)[0]
                result = await shinka_llm.query_async(
                    model_name=self.evo.llm_models[0],
                    msg=f"improve {text}",
                    system_msg=self.evo.task_sys_msg,
                    msg_history=[],
                )
                # The fake model echoes the end of the printed message list; keep only the word.
                return self.init_program.replace(START + self.seed_text, START + result.content.replace("'}]", ""))

            def run(self):
                asyncio.run(self._run())

            async def _run(self):
                (self.results_dir / "evaluate.py").write_text(self.evaluate_str)
                db = sqlite3.connect(self.db_path)
                db.execute("CREATE TABLE programs (id TEXT, generation INTEGER, parent_id TEXT)")
                db.commit()
                self.seed_text = self.init_program.split(START)[1].split(END)[0]
                self.best = (None, self.init_program, float("-inf"))
                for generation in range(self.evo.num_generations):
                    program = self.init_program
                    if generation:
                        program = await self._generate_proposal_async(generation, f"task-{generation}")
                    score = self._evaluate(generation, program)
                    program_id = str(uuid.uuid4())
                    db.execute("INSERT INTO programs VALUES (?, ?, ?)", (program_id, generation, self.best[0]))
                    db.commit()
                    if score > self.best[2]:
                        self.best = (program_id, program, score)

            def _evaluate(self, generation, program):
                folder = self.results_dir / f"gen_{generation}"
                folder.mkdir(parents=True)
                (folder / "main.md").write_text(program)
                subprocess.run(
                    [self.python, str(self.results_dir / "evaluate.py"),
                     "--program_path", str(folder / "main.md"), "--results_dir", str(folder / "results")],
                    check=True,
                )
                metrics = json.loads((folder / "results" / "metrics.json").read_text())
                return metrics.get("combined_score", float("-inf"))
    ''',
}


def _fake_python(tmp_path: Path) -> str:
    """Write the fake upstream package and an interpreter wrapper that imports it.

    Args:
        tmp_path: Pytest's temporary folder.

    Returns:
        The wrapper's path: the engine's ``shinka-evolve`` interpreter.
    """
    root = tmp_path / "fake-upstream"
    for name, text in _FAKE_UPSTREAM.items():
        path = root / name
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(textwrap.dedent(text).lstrip(), encoding="utf-8")
    python = tmp_path / "shinka-python"
    python.write_text(f'#!/bin/sh\nPYTHONPATH={root} exec {sys.executable} "$@"\n', encoding="utf-8")
    python.chmod(python.stat().st_mode | stat.S_IXUSR)
    return str(python)


def _no_sandbox(*args: object) -> None:
    """Fail the test: the engine loop path must not start the sandbox runtime.

    Args:
        *args: ``run_native_engine``'s arguments.

    Raises:
        AssertionError: Always.
    """
    raise AssertionError("The engine loop path called run_native_engine.")


def test_each_upstream_model_reply_and_scorer_command_is_one_loop_step(
    owner: tuple[User, int],
    engine: Engine,
    step_log: StepLogStore,
    trees: TreeStore,
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Run upstream in the child: the loop's model answers each call, and the loop's boxes score each version."""
    monkeypatch.setattr(shinka_evolve, "run_native_engine", _no_sandbox)
    loop, model, boxes = _loop(owner, engine, step_log, trees, tmp_path, generation=1)
    scorer = _scorer(_tree(trees, "README", "seed"), _tree(trees, "metric.txt", "len"))

    result = ShinkaEvolveEngine(_fake_python(tmp_path)).run(
        Task("seed"), loop.server(scorer, max_evals=10), loop.context(max_iterations=2)
    )

    assert (result.best_candidate, result.best_score, result.total_evals) == ("seedxx", 6.0, 3)
    # The fake model costs one cent a call.
    assert result.metadata["model_cost_cents"] == "2"
    assert [prompt.rsplit("'content': ", 1)[-1] for prompt in model.prompts] == [
        "'improve seed'}]",
        "'improve seedx'}]",
    ]
    assert all("'role': 'system'" in prompt for prompt in model.prompts)
    assert boxes.opened == 6
    assert not +boxes.open
    assert [kind for _, kind, _ in _steps(engine)] == _KINDS


def test_a_run_killed_in_a_model_call_resumes_from_its_last_committed_step(
    owner: tuple[User, int], engine: Engine, step_log: StepLogStore, trees: TreeStore, tmp_path: Path
) -> None:
    """Kill the worker in the second model call, then resume: upstream replays, and only new steps run."""
    seed_tree, metric_tree = _tree(trees, "README", "seed"), _tree(trees, "metric.txt", "len")
    python = _fake_python(tmp_path)
    first, first_model, _ = _loop(owner, engine, step_log, trees, tmp_path, generation=1, die_at=1)
    with pytest.raises(_WorkerDiedError):
        ShinkaEvolveEngine(python).run(
            Task("seed"), first.server(_scorer(seed_tree, metric_tree), max_evals=10), first.context(max_iterations=2)
        )
    assert len(first_model.prompts) == 1
    assert len(_steps(engine)) == 5

    _new_generation(engine)
    loop, model, boxes = _loop(owner, engine, step_log, trees, tmp_path, generation=2)
    result = ShinkaEvolveEngine(python).run(
        Task("seed"), loop.server(_scorer(seed_tree, metric_tree), max_evals=10), loop.context(max_iterations=2)
    )

    assert (result.best_candidate, result.best_score) == ("seedxx", 6.0)
    # The committed reply comes back from the log; only the call that never committed runs again.
    assert [prompt.rsplit("'content': ", 1)[-1] for prompt in model.prompts] == ["'improve seedx'}]"]
    assert boxes.opened == 2
    steps = _steps(engine)
    assert [kind for _, kind, _ in steps] == _KINDS
    assert [generation for _, _, generation in steps] == [1] * 5 + [2] * 3


@pytest.mark.parametrize(
    ("settings", "refusal"),
    [({"novelty": True}, "novelty"), ({"editor": "agent"}, "agent editor")],
)
def test_a_feature_the_worker_does_not_run_yet_is_refused_before_any_step(
    settings: dict[str, object], refusal: str, tmp_path: Path
) -> None:
    """Refuse novelty rejection and the agent editor, which need model routes the loop does not give.

    Args:
        settings: The run's ShinkaEvolve settings.
        refusal: Words the refusal names.
        tmp_path: Pytest's temporary folder.
    """
    calls: list[object] = []
    ctx = EngineContext(
        reflection_lm=calls.append,
        run_dir=str(tmp_path),
        call_model=lambda prompt, tools: calls.append(prompt),  # type: ignore[arg-type,return-value]
        native_options=SimpleNamespace(shinka=settings, budget_route=None),  # type: ignore[arg-type]
    )

    with pytest.raises(ServiceError, match=refusal):
        ShinkaEvolveEngine(_fake_python(tmp_path)).run(
            Task("seed"), EvalServer(lambda candidate, case: (calls.append(candidate), (1.0, {}))[1], max_evals=5), ctx
        )
    assert calls == []
    assert not (tmp_path / "shinka_evolve").exists()


def test_with_no_engine_loop_context_the_engine_runs_in_the_sandbox_as_before(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Hand a run with no loop context to ``run_native_engine`` unchanged, until the cutover (SKY-728)."""
    calls: list[tuple[object, ...]] = []
    expected = Result(best_candidate="sandbox", best_score=1.0, total_evals=1)

    def native(*args: object) -> Result:
        """Record the call and return the sandbox's result.

        Args:
            *args: ``run_native_engine``'s arguments.

        Returns:
            The sandbox's result.
        """
        calls.append(args)
        return expected

    monkeypatch.setattr(shinka_evolve, "run_native_engine", native)
    task = Task("seed")
    server = EvalServer(lambda candidate, case: (1.0, {}), max_evals=5)
    ctx = EngineContext(reflection_lm=lambda prompt: "", run_dir=str(tmp_path))

    assert ShinkaEvolveEngine(_fake_python(tmp_path)).run(task, server, ctx) is expected
    assert calls == [("shinka_evolve", task, server, ctx)]
    assert not (tmp_path / "shinka_evolve").exists()


def test_a_budget_stop_carries_the_best_version_scored_so_far(
    owner: tuple[User, int], engine: Engine, step_log: StepLogStore, trees: TreeStore, tmp_path: Path
) -> None:
    """Stop the run in the last version's evaluation, and keep an earlier scored version as the stop's result."""
    loop, _, _ = _loop(owner, engine, step_log, trees, tmp_path, generation=1)
    checks: list[None] = []

    def check_budget() -> None:
        """Stop the run at the first check of the third version's evaluation.

        Raises:
            BudgetReached: At the fifth check.
        """
        checks.append(None)
        if len(checks) == 5:
            raise BudgetReached

    with pytest.raises(BudgetReached) as stopped:
        ShinkaEvolveEngine(_fake_python(tmp_path)).run(
            Task("seed"),
            loop.server(_scorer(_tree(trees, "README", "seed"), _tree(trees, "metric.txt", "len")), max_evals=10),
            loop.context(max_iterations=2, check_budget=check_budget),
        )

    result = stopped.value.result
    assert (result.best_candidate, result.best_score) == ("seedx", 5.0)
    assert result.metadata["model_cost_cents"] == "2"
    assert stopped.value.evidence == {
        "selection_scope": "validation",
        "final_evaluation_completed": False,
        "final_evaluation_reason": "budget_reached",
    }


def test_a_worker_without_the_shinka_interpreter_runs_the_engine_in_the_sandbox(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Fall back to the sandbox runtime when the worker image holds no ``shinka-evolve`` interpreter.

    Args:
        tmp_path: Pytest's temporary folder.
        monkeypatch: Replaces ``run_native_engine``.
    """
    expected = Result(best_candidate="seed", best_score=1.0, total_evals=1)
    calls: list[tuple[object, ...]] = []

    def native(*args: object) -> Result:
        """Record the sandbox call.

        Args:
            *args: ``run_native_engine``'s arguments.

        Returns:
            The sandbox run's result.
        """
        calls.append(args)
        return expected

    monkeypatch.setattr(shinka_evolve, "run_native_engine", native)
    task = Task("seed")
    server = EvalServer(lambda candidate, case: (1.0, {}), max_evals=5)
    ctx = EngineContext(reflection_lm=lambda prompt: "", run_dir=str(tmp_path), call_model=lambda messages, tools: None)

    assert ShinkaEvolveEngine(str(tmp_path / "missing" / "python")).run(task, server, ctx) is expected
    assert calls == [("shinka_evolve", task, server, ctx)]
    assert not (tmp_path / "shinka_evolve").exists()


def _requests(tmp_path: Path, scorer: object) -> shinka_evolve._Requests:
    """Build the engine's request handler around a plain eval server.

    Args:
        tmp_path: Pytest's temporary folder.
        scorer: The eval server's scorer.

    Returns:
        A handler for the nonce ``n``.
    """
    task = Task("seed", cases=[{"input": "a"}])
    ctx = EngineContext(reflection_lm=lambda prompt: "", run_dir=str(tmp_path))
    return shinka_evolve._Requests(task, EvalServer(scorer, max_evals=5), ctx, "n")


def test_a_broken_log_or_progress_line_stays_as_plain_output(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    """Keep a log or progress frame the engine cannot read as plain output, and keep a broken request fatal.

    Args:
        tmp_path: Pytest's temporary folder.
        monkeypatch: Records the plain output lines.
    """
    plain: list[str] = []
    monkeypatch.setattr(sandbox_log, "stream_line", lambda line, **kwargs: plain.append(line))
    requests = _requests(tmp_path, lambda candidate, case: (1.0, {}))
    replies = io.StringIO()

    requests.line("APP_NATIVE_LOG n {broken", replies)
    requests.line("APP_NATIVE_PROGRESS n [1", replies)
    requests.line("APP_NATIVE_PROGRESS n 3", replies)

    assert plain == ["{broken", "[1", "3"]
    assert replies.getvalue() == ""
    with pytest.raises(ValueError):
        requests.line("APP_NATIVE_RPC n {broken", replies)


def test_a_scorer_runs_inside_the_version_and_case_labels(tmp_path: Path) -> None:
    """Label the scorer's run-log events and model calls with the version and case it scores.

    Args:
        tmp_path: Pytest's temporary folder.
    """
    seen: list[tuple[object, object]] = []

    def scorer(candidate: object, case: object) -> tuple[float, dict[str, object]]:
        """Record the log scope and the usage tags the scorer runs in.

        Args:
            candidate: The version.
            case: The case.

        Returns:
            A score with no side information.
        """
        seen.append((sandbox_log._scope.get(), current_tags()))
        return 1.0, {}

    requests = _requests(tmp_path, scorer)
    replies = io.StringIO()

    requests.line(
        'APP_NATIVE_RPC n {"id": "0", "kind": "evaluate", "candidate": "seed", "example": {"id": 0}, "candidate_id": 3}',
        replies,
    )

    scope, tags = seen[0]
    assert (scope["candidate"], scope["case"]) == ("3", "0")
    assert (tags["candidate"], tags["case"]) == ("3", "0")
    assert '"score": 1.0' in replies.getvalue()
