"""Tests for the ShinkaEvolve engine on the worker engine loop (SKY-724).

A small fake ``shinka`` package stands in for upstream: it scores the
starting program, then asks the model for one new version each generation,
through the same module names the real package uses. The runner child runs
in a real process; the loop uses local boxes and a fake model, so the tests
make no network and no paid call.
"""

from __future__ import annotations

import contextlib
import io
import json
import os
import signal
import stat
import sys
import textwrap
import time
from collections.abc import Callable
from decimal import Decimal
from pathlib import Path
from types import SimpleNamespace

import pytest
from sqlalchemy.engine import Engine

from core.billing.usage_tags import current_tags
from core.exceptions import ServiceError
from core.service_gateway.optimization.blackbox import sandbox_log, shinka_evolve
from core.service_gateway.optimization.blackbox.protocol import (
    BudgetExhaustedError,
    EngineContext,
    EvalServer,
    RepoSeed,
    Result,
    Task,
)
from core.service_gateway.optimization.blackbox.shinka_evolve import ShinkaEvolveEngine
from core.service_gateway.optimization.budget_stop import BudgetReached
from core.storage.step_log_store import StepLogStore
from core.storage.tree_store import TreeStore
from core.worker.actors import User
from core.worker.engine_loop import LoopStoppedError, ModelCallFailedError
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
            def __init__(self, python_executable, time=None):
                self.python_executable = python_executable
                self.time = time
    """,
    "shinka/local_openai_config.py": "def parse_local_openai_model(name):\n    return None\n",
    "shinka/prompts.py": "FULL_SYS_FORMATS = {}\n",
    "shinka/core/runner.py": '''
        import asyncio
        import json
        import os
        import random
        import sqlite3
        import subprocess
        import uuid
        from pathlib import Path
        from urllib.parse import parse_qsl, urlparse

        from shinka.llm import llm as shinka_llm

        START = "<!-- EVOLVE-BLOCK-START -->\\n"
        END = "\\n<!-- EVOLVE-BLOCK-END -->"
        # The generation at which upstream fails, or None for a run that ends normally.
        FAIL_AFTER = None
        # Whether upstream leaves results larger than the runner's artifact archive limit.
        BIG_RESULTS = False


        class EvolutionConfig:
            def __init__(self, **fields):
                self.__dict__.update(fields)


        class ShinkaEvolveRunner:
            """Score the start, then one model-written version per generation, one at a time."""

            def __init__(self, evo_config, job_config, db_config, init_program_str, evaluate_str, **runner):
                # Upstream 0.0.7 refuses a local model whose api_key_env variable is unset.
                models = list(evo_config.llm_models)
                if evo_config.meta_rec_interval and evo_config.meta_llm_models:
                    models += evo_config.meta_llm_models
                for model in models:
                    for key, value in parse_qsl(urlparse(model.split("@", 1)[1]).query):
                        if key == "api_key_env" and not os.getenv(value, "").strip():
                            raise ValueError(f"The model {model} needs the unset variable {value}.")
                self.evo = evo_config
                self.python = job_config.python_executable
                self.db_path = db_config.db_path
                self.init_program = init_program_str
                self.evaluate_str = evaluate_str
                self.results_dir = Path(evo_config.results_dir)
                self.running_jobs = []
                self.failed_jobs_for_retry = {}

            def _get_completed_job_work_count(self):
                return 0

            def _has_background_side_effect_work(self):
                return False

            async def _generate_proposal_async(self, generation, task_id):
                text = self.best[1].split(START)[1].split(END)[0]
                # Upstream's prompts carry random draws and program ids, so a
                # replay matches its log only when the runner seeds them.
                result = await shinka_llm.query_async(
                    model_name=self.evo.llm_models[0],
                    msg=f"improve {text}",
                    system_msg=f"{self.evo.task_sys_msg} {random.random()} {uuid.uuid4()}",
                    msg_history=[],
                )
                # The fake model echoes the end of the printed message list; keep only the word.
                return self.init_program.replace(START + self.seed_text, START + result.content.replace("'}]", ""))

            def run(self):
                asyncio.run(self._run())

            async def _run(self):
                (self.results_dir / "evaluate.py").write_text(self.evaluate_str)
                if BIG_RESULTS:
                    # A sparse file: past the 64 MiB limit by its size, with no disk use.
                    with (self.results_dir / "big.bin").open("wb") as big:
                        big.truncate(64 * 1024 * 1024 + 1)
                db = sqlite3.connect(self.db_path)
                db.execute("CREATE TABLE programs (id TEXT, generation INTEGER, parent_id TEXT)")
                db.commit()
                self.seed_text = self.init_program.split(START)[1].split(END)[0]
                self.best = (None, self.init_program, float("-inf"))
                for generation in range(self.evo.num_generations):
                    if generation == FAIL_AFTER:
                        raise RuntimeError("upstream broke")
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


def _fake_python(tmp_path: Path, fail_after: int | None = None, big_results: bool = False) -> str:
    """Write the fake upstream package and an interpreter wrapper that imports it.

    Args:
        tmp_path: Pytest's temporary folder.
        fail_after: The generation at which the fake upstream fails, or None for no failure.
        big_results: Whether the fake upstream leaves results larger than the artifact archive limit.

    Returns:
        The wrapper's path: the engine's ``shinka-evolve`` interpreter.
    """
    root = tmp_path / "fake-upstream"
    for name, text in _FAKE_UPSTREAM.items():
        path = root / name
        path.parent.mkdir(parents=True, exist_ok=True)
        text = (
            textwrap.dedent(text)
            .lstrip()
            .replace("FAIL_AFTER = None", f"FAIL_AFTER = {fail_after}")
            .replace("BIG_RESULTS = False", f"BIG_RESULTS = {big_results}")
        )
        path.write_text(text, encoding="utf-8")
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
    """Run upstream in the child: the loop's model answers each call, and the loop's boxes score each version.

    Args:
        owner: Alice and her budget generation.
        engine: The database.
        step_log: The step log.
        trees: The tree store.
        tmp_path: Pytest's temporary folder.
        monkeypatch: Replaces ``run_native_engine``.
    """
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
    assert not any(boxes.open.values())
    assert [kind for _, kind, _ in _steps(engine)] == _KINDS


def test_a_run_killed_in_a_model_call_resumes_from_its_last_committed_step(
    owner: tuple[User, int], engine: Engine, step_log: StepLogStore, trees: TreeStore, tmp_path: Path
) -> None:
    """Kill the worker in the second model call, then resume: upstream replays, and only new steps run.

    Args:
        owner: Alice and her budget generation.
        engine: The database.
        step_log: The step log.
        trees: The tree store.
        tmp_path: Pytest's temporary folder.
    """
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


@pytest.mark.parametrize(
    ("seed_hidden", "error"),
    [({"h1": "file"}, BudgetExhaustedError), ({"h2": "file"}, ServiceError), ({}, ServiceError), (None, ServiceError)],
)
def test_the_repository_options_must_hide_the_same_paths_as_the_runs_repository_seed(
    seed_hidden: dict[str, str] | None, error: type[Exception], tmp_path: Path
) -> None:
    """Refuse options whose hidden paths differ from the seed's, which the loop checks against its own (SKY-719).

    With the same hidden paths, the run goes on to its next check: here, an empty evaluation budget.
    Options with no repository seed have nothing to check against, so the run refuses them too.

    Args:
        seed_hidden: The repository seed's hidden path hashes, or None for a run with no repository seed.
        error: The error the run raises.
        tmp_path: Pytest's temporary folder.
    """
    repo = {"chunks": [], "editable_paths": [], "readonly_paths": [], "hidden_hashes": {"h1": "file"}}
    ctx = EngineContext(
        reflection_lm=lambda prompt: "",
        run_dir=str(tmp_path),
        call_model=lambda messages, tools: None,  # type: ignore[arg-type,return-value]
        native_options=SimpleNamespace(shinka={}, budget_route=None, repo=repo),  # type: ignore[arg-type]
        repo_seed=None if seed_hidden is None else RepoSeed("tree", hidden_hashes=seed_hidden),
    )

    with pytest.raises(error):
        ShinkaEvolveEngine(_fake_python(tmp_path)).run(
            Task("seed"), EvalServer(lambda candidate, case: (1.0, {}), max_evals=0), ctx
        )
    assert not (tmp_path / "shinka_evolve").exists()


def test_with_no_engine_loop_context_the_engine_runs_in_the_sandbox_as_before(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Hand a run with no loop context to ``run_native_engine`` unchanged, until the cutover (SKY-728).

    Args:
        tmp_path: Pytest's temporary folder.
        monkeypatch: Replaces ``run_native_engine``.
    """
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
    """Stop the run in the last version's evaluation, and keep an earlier scored version as the stop's result.

    Args:
        owner: Alice and her budget generation.
        engine: The database.
        step_log: The step log.
        trees: The tree store.
        tmp_path: Pytest's temporary folder.
    """
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


def _five_checks_then(error: Exception) -> Callable[[], None]:
    """Return a budget check that raises at its fifth call: the first check of the third version's evaluation.

    Args:
        error: The error the check raises.

    Returns:
        The budget check.
    """
    checks: list[None] = []

    def check_budget() -> None:
        """Count the check, and raise at the fifth.

        Raises:
            Exception: ``error``, at the fifth check.
        """
        checks.append(None)
        if len(checks) == 5:
            raise error

    return check_budget


def test_a_budget_stop_reports_the_runs_evaluations_not_those_when_the_best_was_found(
    owner: tuple[User, int], engine: Engine, step_log: StepLogStore, trees: TreeStore, tmp_path: Path
) -> None:
    """Report two evaluations when the stop comes after the second version, though the first version is the best.

    Args:
        owner: Alice and her budget generation.
        engine: The database.
        step_log: The step log.
        trees: The tree store.
        tmp_path: Pytest's temporary folder.
    """
    loop, _, _ = _loop(owner, engine, step_log, trees, tmp_path, generation=1)
    by_length = _scorer(_tree(trees, "README", "seed"), _tree(trees, "metric.txt", "len"))

    def shortest(step_loop: object, candidate: object, case: object) -> tuple[float | None, dict[str, object]]:
        """Score a shorter version higher, so the seed stays the best version.

        Args:
            step_loop: The engine loop.
            candidate: The version.
            case: Not used.

        Returns:
            The negative length and no side information.
        """
        score, info = by_length(step_loop, candidate, case)
        return -score, info

    with pytest.raises(BudgetReached) as stopped:
        ShinkaEvolveEngine(_fake_python(tmp_path)).run(
            Task("seed"),
            loop.server(shortest, max_evals=10),
            loop.context(max_iterations=2, check_budget=_five_checks_then(BudgetReached())),
        )

    result = stopped.value.result
    assert (result.best_candidate, result.best_score, result.total_evals) == ("seed", -4.0, 2)


def test_the_scorer_cap_mid_run_returns_the_best_version_scored_so_far(
    owner: tuple[User, int], engine: Engine, step_log: StepLogStore, trees: TreeStore, tmp_path: Path
) -> None:
    """End the run with the best scored version when the parent's scorer cap stops the third evaluation.

    Args:
        owner: Alice and her budget generation.
        engine: The database.
        step_log: The step log.
        trees: The tree store.
        tmp_path: Pytest's temporary folder.
    """
    loop, _, _ = _loop(owner, engine, step_log, trees, tmp_path, generation=1)

    result = ShinkaEvolveEngine(_fake_python(tmp_path)).run(
        Task("seed"),
        loop.server(_scorer(_tree(trees, "README", "seed"), _tree(trees, "metric.txt", "len")), max_evals=10),
        loop.context(max_iterations=2, check_budget=_five_checks_then(BudgetExhaustedError("cap"))),
    )

    assert (result.best_candidate, result.best_score, result.total_evals) == ("seedx", 5.0, 2)
    assert result.metadata["model_cost_cents"] == "2"


def test_a_child_that_fails_after_a_scored_version_returns_that_version(
    owner: tuple[User, int], engine: Engine, step_log: StepLogStore, trees: TreeStore, tmp_path: Path
) -> None:
    """Return the version scored before upstream failed, as the sandbox path does, from a child that exits non-zero.

    Args:
        owner: Alice and her budget generation.
        engine: The database.
        step_log: The step log.
        trees: The tree store.
        tmp_path: Pytest's temporary folder.
    """
    loop, _, _ = _loop(owner, engine, step_log, trees, tmp_path, generation=1)

    result = ShinkaEvolveEngine(_fake_python(tmp_path, fail_after=1)).run(
        Task("seed"),
        loop.server(_scorer(_tree(trees, "README", "seed"), _tree(trees, "metric.txt", "len")), max_evals=10),
        loop.context(max_iterations=2),
    )

    assert (result.best_candidate, result.best_score, result.total_evals) == ("seed", 4.0, 1)
    assert "upstream broke" in (tmp_path / "loop-1" / "shinka_evolve" / "native_result.json").read_text()


def test_on_the_engine_loop_results_past_the_archive_limit_do_not_fail_the_run(
    owner: tuple[User, int], engine: Engine, step_log: StepLogStore, trees: TreeStore, tmp_path: Path
) -> None:
    """Skip the artifact archive on the engine loop, where nothing reads it, so large results keep the run's result.

    Args:
        owner: Alice and her budget generation.
        engine: The database.
        step_log: The step log.
        trees: The tree store.
        tmp_path: Pytest's temporary folder.
    """
    loop, _, _ = _loop(owner, engine, step_log, trees, tmp_path, generation=1)

    result = ShinkaEvolveEngine(_fake_python(tmp_path, big_results=True)).run(
        Task("seed"),
        loop.server(_scorer(_tree(trees, "README", "seed"), _tree(trees, "metric.txt", "len")), max_evals=10),
        loop.context(max_iterations=2),
    )

    workdir = tmp_path / "loop-1" / "shinka_evolve"
    assert (result.best_candidate, result.best_score) == ("seedxx", 6.0)
    assert "error" not in json.loads((workdir / "native_result.json").read_text())
    assert not (workdir / "native_artifacts.tar.gz.b64").exists()


def test_a_child_that_fails_before_any_scored_version_fails_the_run(
    owner: tuple[User, int], engine: Engine, step_log: StepLogStore, trees: TreeStore, tmp_path: Path
) -> None:
    """Fail the run when upstream fails before it scores a version: a version with no score is never the best.

    Args:
        owner: Alice and her budget generation.
        engine: The database.
        step_log: The step log.
        trees: The tree store.
        tmp_path: Pytest's temporary folder.
    """
    loop, _, _ = _loop(owner, engine, step_log, trees, tmp_path, generation=1)

    with pytest.raises(ServiceError, match="upstream broke"):
        ShinkaEvolveEngine(_fake_python(tmp_path, fail_after=0)).run(
            Task("seed"),
            loop.server(_scorer(_tree(trees, "README", "seed"), _tree(trees, "metric.txt", "len")), max_evals=10),
            loop.context(max_iterations=2),
        )


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


def _requests(
    tmp_path: Path, scorer: object, call_model: Callable[..., object] | None = None
) -> shinka_evolve._Requests:
    """Build the engine's request handler around a plain eval server.

    Args:
        tmp_path: Pytest's temporary folder.
        scorer: The eval server's scorer.
        call_model: The loop's model call, or None for a run that makes none.

    Returns:
        A handler for the nonce ``n``.
    """
    task = Task("seed", cases=[{"input": "a"}])
    ctx = EngineContext(
        reflection_lm=lambda prompt: "",
        run_dir=str(tmp_path),
        call_model=call_model,  # type: ignore[arg-type]
    )
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


def _model_request(request_id: str) -> str:
    """Frame one model request line of the child, for the nonce ``n``.

    Args:
        request_id: The request's id.

    Returns:
        The framed line.
    """
    return "APP_NATIVE_RPC n " + json.dumps(
        {"id": request_id, "kind": "model", "messages": [{"role": "user", "content": "improve"}]}
    )


def test_a_failed_model_call_adds_its_billed_cost_to_the_runs_model_cost(tmp_path: Path) -> None:
    """Count the cost of a failed call that the provider billed, as its committed step keeps it on a replay.

    Args:
        tmp_path: Pytest's temporary folder.
    """
    calls: list[None] = []

    def call_model(messages: object, tools: object) -> SimpleNamespace:
        """Fail the first call with a billed cost, and answer the second.

        Args:
            messages: The chat messages.
            tools: Not used.

        Returns:
            The reply of the second call.

        Raises:
            ModelCallFailedError: At the first call, with a cost of 3 cents.
        """
        calls.append(None)
        if len(calls) == 1:
            raise ModelCallFailedError("TimeoutError", cost_cents=Decimal(3))
        return SimpleNamespace(text="ok", cost_cents=Decimal(2))

    requests = _requests(tmp_path, lambda candidate, case: (1.0, {}), call_model)
    replies = io.StringIO()

    requests.line(_model_request("1"), replies)
    requests.line(_model_request("2"), replies)

    assert [json.loads(line) for line in replies.getvalue().splitlines()] == [
        {"id": "1", "error": "ModelCallFailedError"},
        {"id": "2", "text": "ok"},
    ]
    requests.best = {"candidate": "seed", "score": 1.0}
    assert requests.best_so_far(tmp_path).metadata["model_cost_cents"] == "5"


def test_an_error_of_the_model_call_that_is_not_a_failed_call_ends_the_run(tmp_path: Path) -> None:
    """Raise an error of the loop itself to the run, and do not send it to upstream as a failed call.

    Args:
        tmp_path: Pytest's temporary folder.
    """

    def call_model(messages: object, tools: object) -> SimpleNamespace:
        """Fail as a broken loop does.

        Args:
            messages: The chat messages.
            tools: Not used.

        Raises:
            TypeError: Always.
        """
        raise TypeError("the loop broke")

    requests = _requests(tmp_path, lambda candidate, case: (1.0, {}), call_model)
    replies = io.StringIO()

    with pytest.raises(TypeError, match="the loop broke"):
        requests.line(_model_request("1"), replies)
    assert replies.getvalue() == ""


def _scored_requests(tmp_path: Path) -> shinka_evolve._Requests:
    """Build a request handler whose eval server scored ``good`` at 2 and gave ``bad`` a metric error.

    Args:
        tmp_path: Pytest's temporary folder.

    Returns:
        The handler.
    """
    requests = _requests(tmp_path, lambda candidate, case: (None, {}) if candidate == "bad" else (2.0, {}))
    for candidate in ("bad", "good"):
        requests.server.evaluate(candidate, requests.task.cases[0])
    return requests


def _report(requests: shinka_evolve._Requests, candidate: str, score: float, candidate_id: int) -> None:
    """Send the progress line of one scored version, as the child reports it.

    Args:
        requests: The request handler.
        candidate: The version.
        score: The score the child reports.
        candidate_id: The version's id.
    """
    event = {"event": "candidate", "candidate_id": candidate_id, "candidate": candidate, "score": score}
    requests.line("APP_NATIVE_PROGRESS n " + json.dumps(event), io.StringIO())


def _write_result(workdir: Path, document: dict[str, object] | None) -> None:
    """Write the child's folder with its result document and an empty error log.

    Args:
        workdir: The child's folder.
        document: The result document, or None for a child that wrote none.
    """
    workdir.mkdir()
    (workdir / "stderr.log").write_text("")
    if document is not None:
        (workdir / "native_result.json").write_text(json.dumps(document))


def test_a_reported_version_with_a_metric_error_has_no_score(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    """Show a version with a crashed case with no score, and never keep it as the best version (SKY-350).

    Args:
        tmp_path: Pytest's temporary folder.
        monkeypatch: Records the shown versions.
    """
    shown: list[object] = []
    monkeypatch.setattr(shinka_evolve, "emit_candidate", lambda callback, **fields: shown.append(fields["score"]))
    requests = _scored_requests(tmp_path)

    _report(requests, "bad", 3.0, 1)

    assert shown == [None]
    assert requests.best is None


def test_an_incumbent_with_a_metric_error_is_not_the_result(tmp_path: Path) -> None:
    """Fail the run when upstream failed and its only incumbent hit a metric error.

    Args:
        tmp_path: Pytest's temporary folder.
    """
    workdir = tmp_path / "child"
    incumbent = {"best_candidate": "bad", "best_score": 3.0, "total_evals": 2}
    _write_result(workdir, {"error": "upstream broke", "interrupted_incumbent": incumbent})

    with pytest.raises(ServiceError, match="upstream broke"):
        ShinkaEvolveEngine._result(workdir, 1, _scored_requests(tmp_path))


@pytest.mark.parametrize(
    "document",
    [
        {
            "error": "upstream broke",
            "interrupted_incumbent": {"best_candidate": "bad", "best_score": 3.0, "total_evals": 2},
        },
        None,
    ],
    ids=["incumbent-with-a-metric-error", "no-result-file"],
)
def test_with_no_valid_result_the_run_keeps_the_best_reported_version(
    document: dict[str, object] | None, tmp_path: Path
) -> None:
    """Return the best version the child reported when its result holds no valid version, or it wrote none.

    Args:
        document: The child's result document, or None for a killed child that wrote none.
        tmp_path: Pytest's temporary folder.
    """
    workdir = tmp_path / "child"
    _write_result(workdir, document)
    requests = _scored_requests(tmp_path)
    _report(requests, "good", 2.0, 1)

    result = ShinkaEvolveEngine._result(workdir, 1 if document else -9, requests)

    assert (result.best_candidate, result.best_score) == ("good", 2.0)


def _scripted_child(tmp_path: Path, body: str) -> str:
    """Write an interpreter wrapper whose child runs ``body`` in place of the runner.

    The body sees ``nonce``, read from the engine's input document, and the
    ``emit`` function that writes one framed line.

    Args:
        tmp_path: Pytest's temporary folder.
        body: The child's Python code.

    Returns:
        The wrapper's path: the engine's ``shinka-evolve`` interpreter.
    """
    script = tmp_path / "child.py"
    head = """
        import json
        import subprocess
        import sys
        import time

        nonce = json.load(open(sys.argv[2]))["nonce"]


        def emit(prefix, payload):
            print(f"{prefix}{nonce} {json.dumps(payload)}", flush=True)
    """
    script.write_text(textwrap.dedent(head) + textwrap.dedent(body), encoding="utf-8")
    python = tmp_path / "shinka-python"
    python.write_text(f'#!/bin/sh\nexec {sys.executable} {script} "$@"\n', encoding="utf-8")
    python.chmod(python.stat().st_mode | stat.S_IXUSR)
    return str(python)


def _stopping_request(error: BaseException) -> Callable[[shinka_evolve._Requests, dict[str, object]], None]:
    """Return a request handler that stops the run at the child's first request.

    Args:
        error: The stop the handler raises.

    Returns:
        The handler, in place of ``_Requests._request``.
    """

    def stop(self: shinka_evolve._Requests, request: dict[str, object]) -> None:
        """Stop the run.

        Args:
            self: The request handler.
            request: The child's request, ignored.

        Raises:
            BaseException: ``error``, always.
        """
        raise error

    return stop


def _context(tmp_path: Path) -> EngineContext:
    """Build an engine-loop context whose model is never called.

    Args:
        tmp_path: Pytest's temporary folder, the run folder.

    Returns:
        The context.
    """
    return EngineContext(reflection_lm=lambda prompt: "", run_dir=str(tmp_path), call_model=lambda *args: None)  # type: ignore[arg-type]


def test_a_stopped_run_also_kills_the_evaluation_jobs_its_child_started(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Kill the child's evaluation job too when the loop stops the run, so it cannot outlive the run.

    Upstream starts ``evaluate.py`` as a child of the runner. Left alive, it
    polls for days and writes into the folder a resume starts again.

    Args:
        tmp_path: Pytest's temporary folder.
        monkeypatch: Stops the run at the child's first request.
    """
    job_pid = tmp_path / "job.pid"
    python = _scripted_child(
        tmp_path,
        f"""
        job = subprocess.Popen(
            [sys.executable, "-c", "import time; time.sleep(60)"],
            stdout=subprocess.DEVNULL,
            stderr=subprocess.DEVNULL,
        )
        open({str(job_pid)!r}, "w").write(str(job.pid))
        emit("APP_NATIVE_RPC ", {{"kind": "evaluate", "id": "0"}})
        time.sleep(60)
        """,
    )
    monkeypatch.setattr(shinka_evolve._Requests, "_request", _stopping_request(LoopStoppedError("lease lost")))
    pid = 0
    try:
        with pytest.raises(LoopStoppedError):
            ShinkaEvolveEngine(python).run(
                Task("seed"), EvalServer(lambda candidate, case: (1.0, {}), max_evals=5), _context(tmp_path)
            )
        pid = int(job_pid.read_text())
        deadline = time.monotonic() + 10
        while time.monotonic() < deadline:
            try:
                os.kill(pid, 0)
            except ProcessLookupError:
                break
            time.sleep(0.05)
        else:
            pytest.fail("The evaluation job outlived the stopped run.")
    finally:
        if pid:
            with contextlib.suppress(ProcessLookupError):
                os.kill(pid, signal.SIGKILL)


def test_a_budget_stop_reports_the_best_version_the_child_reports_only_after_the_stop(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Read the child's progress after a budget stop, so a version it reports while it drains is the stop's result.

    Args:
        tmp_path: Pytest's temporary folder.
        monkeypatch: Stops the run at the child's first request.
    """
    python = _scripted_child(
        tmp_path,
        """
        emit("APP_NATIVE_RPC ", {"kind": "evaluate", "id": "0"})
        sys.stdin.read()
        emit("APP_NATIVE_PROGRESS ", {"event": "candidate", "candidate_id": 1, "candidate": "late", "score": 3.0})
        sys.exit(1)
        """,
    )
    monkeypatch.setattr(shinka_evolve._Requests, "_request", _stopping_request(BudgetReached()))

    with pytest.raises(BudgetReached) as stopped:
        ShinkaEvolveEngine(python).run(
            Task("seed"), EvalServer(lambda candidate, case: (1.0, {}), max_evals=5), _context(tmp_path)
        )

    assert stopped.value.result is not None
    assert (stopped.value.result.best_candidate, stopped.value.result.best_score) == ("late", 3.0)
