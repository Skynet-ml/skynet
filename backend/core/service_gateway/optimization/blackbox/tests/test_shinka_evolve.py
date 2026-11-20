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
import resource
import shlex
import signal
import stat
import sys
import textwrap
import time
from collections.abc import Callable
from dataclasses import replace
from decimal import Decimal
from pathlib import Path
from types import SimpleNamespace
from typing import Any

import pytest
from sqlalchemy.engine import Engine

from core.billing.model_gateway import MCP_RELAY_ROUTE_KEY
from core.billing.usage_tags import current_tags
from core.exceptions import ServiceError
from core.i18n import t
from core.sandbox.grants import Grant, UnknownCapabilityError
from core.sandbox.trees import GrantDeniedError
from core.service_gateway.optimization.blackbox import sandbox_log, shinka_bundle, shinka_evolve
from core.service_gateway.optimization.blackbox.protocol import (
    BudgetExhaustedError,
    EngineContext,
    EvalServer,
    ModelReply,
    RepoSeed,
    Task,
)
from core.service_gateway.optimization.blackbox.shinka_evolve import ShinkaEvolveEngine
from core.service_gateway.optimization.budget_stop import BudgetReached
from core.storage.step_log_store import StepLogStore
from core.storage.tree_store import TreeStore
from core.worker.actors import User
from core.worker.engine_loop import LoopStoppedError, ModelCallBilledError, ModelCallFailedError
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


@pytest.fixture(autouse=True)
def _short_reply_wait(monkeypatch: pytest.MonkeyPatch) -> None:
    """Bound a real child's wait for a reply, so a broken stop fails a test instead of hanging it.

    The engine lets a child wait days for a reply. A regression that leaves the
    worker waiting on such a child would then hang the suite.

    Args:
        monkeypatch: Sets the child's reply wait.
    """
    monkeypatch.setattr(shinka_evolve, "_TIMEOUT_SECONDS", 60.0)


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
    "shinka/prompts.py": "FULL_SYS_FORMATS = {}\n",
    "shinka/core/runner.py": '''
        import asyncio
        import json
        import os
        import random
        import sqlite3
        import subprocess
        import sys
        import threading
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
        # Whether a thread prints Rich-like rows to stdout while the runner sends each request.
        NOISY = False


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
                self.bursts = threading.Semaphore(0)

            def _get_completed_job_work_count(self):
                return 0

            def _has_background_side_effect_work(self):
                return False

            async def _generate_proposal_async(self, generation, task_id):
                text = self.best[1].split(START)[1].split(END)[0]
                self.bursts.release()
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
                def noise():
                    # Upstream prints Rich tables from its own threads, one write per
                    # row, while the runner sends a request; each burst is bounded.
                    while self.bursts.acquire():
                        for _ in range(3000):
                            sys.stdout.write("| " + "-" * 120 + " |\\n")

                if NOISY:
                    # Frequent thread switches, so a print can land inside a frame.
                    sys.setswitchinterval(1e-6)
                    threading.Thread(target=noise, daemon=True).start()
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
                self.bursts.release()
                subprocess.run(
                    [self.python, str(self.results_dir / "evaluate.py"),
                     "--program_path", str(folder / "main.md"), "--results_dir", str(folder / "results")],
                    check=True,
                )
                metrics = json.loads((folder / "results" / "metrics.json").read_text())
                return metrics.get("combined_score", float("-inf"))
    ''',
}


def _fake_python(tmp_path: Path, fail_after: int | None = None, big_results: bool = False, noisy: bool = False) -> str:
    """Write the fake upstream package and an interpreter wrapper that imports it.

    Args:
        tmp_path: Pytest's temporary folder.
        fail_after: The generation at which the fake upstream fails, or None for no failure.
        big_results: Whether the fake upstream leaves results larger than the artifact archive limit.
        noisy: Whether the fake upstream prints rows to stdout from another thread during each request.

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
            .replace("NOISY = False", f"NOISY = {noisy}")
        )
        path.write_text(text, encoding="utf-8")
    python = tmp_path / "shinka-python"
    python.write_text(f'#!/bin/sh\nPYTHONPATH={root} exec {sys.executable} "$@"\n', encoding="utf-8")
    python.chmod(python.stat().st_mode | stat.S_IXUSR)
    return str(python)


def test_each_upstream_model_reply_and_scorer_command_is_one_loop_step(
    owner: tuple[User, int], engine: Engine, step_log: StepLogStore, trees: TreeStore, tmp_path: Path
) -> None:
    """Run upstream in the child: the loop's model answers each call, and the loop's boxes score each version.

    Args:
        owner: Alice and her budget generation.
        engine: The database.
        step_log: The step log.
        trees: The tree store.
        tmp_path: Pytest's temporary folder.
    """
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


def test_upstream_stdout_output_from_another_thread_cannot_break_a_frame(
    owner: tuple[User, int],
    engine: Engine,
    step_log: StepLogStore,
    trees: TreeStore,
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Keep every request and progress frame whole while upstream prints large blocks to stdout.

    Args:
        owner: Alice and her budget generation.
        engine: The database.
        step_log: The step log.
        trees: The tree store.
        tmp_path: Pytest's temporary folder.
        monkeypatch: Records unparsed frames.
    """
    broken: list[str] = []
    monkeypatch.setattr(sandbox_log, "stream_line", lambda line, **kwargs: broken.append(line))
    loop, _, _ = _loop(owner, engine, step_log, trees, tmp_path, generation=1)
    scorer = _scorer(_tree(trees, "README", "seed"), _tree(trees, "metric.txt", "len"))

    result = ShinkaEvolveEngine(_fake_python(tmp_path, noisy=True)).run(
        Task("seed"), loop.server(scorer, max_evals=10), loop.context(max_iterations=2)
    )

    assert (result.best_candidate, result.best_score, result.total_evals) == ("seedxx", 6.0, 3)
    # A progress or log frame the worker cannot parse goes to ``stream_line``.
    assert broken == []
    assert [kind for _, kind, _ in _steps(engine)] == _KINDS
    # Upstream's output goes to the stderr log, not into the frames.
    assert "| ---" in (Path(result.metadata["native_artifacts_dir"]) / "stderr.log").read_text(encoding="utf-8")


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


@pytest.mark.parametrize("settings", [{"novelty": True}, {"editor": "agent"}])
def test_novelty_rejection_and_the_agent_editor_run_on_the_engine_loop(
    settings: dict[str, object],
    owner: tuple[User, int],
    engine: Engine,
    step_log: StepLogStore,
    trees: TreeStore,
    tmp_path: Path,
) -> None:
    """Run novelty rejection and the agent editor on the loop, with no refusal and no sandbox fallback (SKY-830).

    Args:
        settings: The run's ShinkaEvolve settings.
        owner: Alice and her budget generation.
        engine: The database.
        step_log: The step log.
        trees: The tree store.
        tmp_path: Pytest's temporary folder.
    """
    loop, _, _ = _loop(owner, engine, step_log, trees, tmp_path, generation=1)
    options = SimpleNamespace(shinka=settings, budget_route=None, repo=None, shinka_models=None, model="m")
    scorer = _scorer(_tree(trees, "README", "seed"), _tree(trees, "metric.txt", "len"))

    result = ShinkaEvolveEngine(_fake_python(tmp_path)).run(
        Task("seed"), loop.server(scorer, max_evals=10), loop.context(max_iterations=2, native_options=options)
    )

    assert (result.best_candidate, result.best_score) == ("seedxx", 6.0)
    assert [kind for _, kind, _ in _steps(engine)] == _KINDS


def test_proposer_mcp_servers_without_the_agent_editor_are_refused_before_any_step(tmp_path: Path) -> None:
    """Refuse ``[proposer] mcp`` servers unless the agent editor runs, since no other mutation uses tools (SKY-830).

    Args:
        tmp_path: Pytest's temporary folder.
    """
    calls: list[object] = []
    ctx = EngineContext(
        reflection_lm=calls.append,
        run_dir=str(tmp_path),
        call_model=lambda prompt, tools: calls.append(prompt),  # type: ignore[arg-type,return-value]
        native_options=SimpleNamespace(  # type: ignore[arg-type]
            shinka={"novelty": True}, budget_route={MCP_RELAY_ROUTE_KEY: {"servers": ["docs"]}}, repo=None
        ),
    )

    with pytest.raises(ServiceError, match=r"\[proposer\] mcp servers only with its agent editor"):
        ShinkaEvolveEngine(_fake_python(tmp_path)).run(
            Task("seed"), EvalServer(lambda candidate, case: (calls.append(candidate), (1.0, {}))[1], max_evals=5), ctx
        )
    assert calls == []
    assert not (tmp_path / "shinka_evolve").exists()


def test_proposer_services_with_the_agent_editor_are_refused_before_any_step(tmp_path: Path) -> None:
    """Refuse ``[proposer] services`` with the agent editor, whose box cannot reach them (SKY-830).

    Args:
        tmp_path: Pytest's temporary folder.
    """
    calls: list[object] = []
    ctx = EngineContext(
        reflection_lm=calls.append,
        run_dir=str(tmp_path),
        call_model=lambda prompt, tools: calls.append(prompt),  # type: ignore[arg-type,return-value]
        native_options=SimpleNamespace(  # type: ignore[arg-type]
            shinka={"editor": "agent"}, budget_route={MCP_RELAY_ROUTE_KEY: {"services": ["db"]}}, repo=None
        ),
    )

    with pytest.raises(ServiceError, match=r"agent editor cannot give the \[proposer\] services"):
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


def test_with_no_engine_loop_context_the_run_is_refused(tmp_path: Path) -> None:
    """Refuse a run with no loop context: no sandbox runtime runs the engine (SKY-728).

    Args:
        tmp_path: Pytest's temporary folder.
    """
    server = EvalServer(lambda candidate, case: (1.0, {}), max_evals=5)
    ctx = EngineContext(reflection_lm=lambda prompt: "", run_dir=str(tmp_path))

    with pytest.raises(ServiceError, match="only on the worker engine loop"):
        ShinkaEvolveEngine(_fake_python(tmp_path)).run(Task("seed"), server, ctx)
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


def test_a_run_starts_in_an_empty_child_folder(
    owner: tuple[User, int], engine: Engine, step_log: StepLogStore, trees: TreeStore, tmp_path: Path
) -> None:
    """Delete the files a previous try left in the child's folder, so upstream starts from scratch.

    Args:
        owner: Alice and her budget generation.
        engine: The database.
        step_log: The step log.
        trees: The tree store.
        tmp_path: Pytest's temporary folder.
    """
    loop, _, _ = _loop(owner, engine, step_log, trees, tmp_path, generation=1)
    stale = tmp_path / "loop-1" / shinka_evolve._WORKDIR / "stale.txt"
    stale.parent.mkdir(parents=True)
    stale.write_text("from the last try")

    result = ShinkaEvolveEngine(_fake_python(tmp_path)).run(
        Task("seed"),
        loop.server(_scorer(_tree(trees, "README", "seed"), _tree(trees, "metric.txt", "len")), max_evals=10),
        loop.context(max_iterations=2),
    )

    assert result.best_candidate == "seedxx"
    assert not stale.exists()


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


def test_a_worker_without_the_shinka_interpreter_fails_the_loop_run_with_a_clear_error(tmp_path: Path) -> None:
    """Fail a loop run when the worker image holds no ``shinka-evolve`` interpreter, with no sandbox run (SKY-830).

    Args:
        tmp_path: Pytest's temporary folder.
    """
    calls: list[object] = []
    server = EvalServer(lambda candidate, case: (calls.append(candidate), (1.0, {}))[1], max_evals=5)
    ctx = EngineContext(
        reflection_lm=lambda prompt: "",
        run_dir=str(tmp_path),
        call_model=lambda messages, tools: calls.append(messages),  # type: ignore[arg-type,return-value]
        native_options=SimpleNamespace(  # type: ignore[arg-type]
            shinka={"editor": "agent"}, budget_route=None, repo=None
        ),
    )

    with pytest.raises(ServiceError) as raised:
        ShinkaEvolveEngine(str(tmp_path / "missing" / "python")).run(Task("seed"), server, ctx)

    assert str(raised.value) == t("blackbox.shinka_interpreter_missing")
    assert str(raised.value) != "blackbox.shinka_interpreter_missing"
    assert calls == []
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
    """Keep a log or progress frame the engine cannot read as plain output, and fail the run on a broken request.

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
    for body in ("{broken", "[1]"):
        with pytest.raises(ServiceError, match="malformed request"):
            requests.line(f"APP_NATIVE_RPC n {body}", replies)


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
        'APP_NATIVE_RPC n {"id": "0", "kind": "evaluate", "candidate": "seed", '
        '"example": {"id": 0}, "candidate_id": 3}',
        replies,
    )

    scope, tags = seen[0]
    assert (scope["candidate"], scope["case"]) == ("3", "0")
    assert (tags["candidate"], tags["case"]) == ("3", "0")
    assert '"score": 1.0' in replies.getvalue()


def test_a_reply_that_json_cannot_encode_goes_back_as_an_error(tmp_path: Path) -> None:
    """Send an error reply when the score cannot be encoded, so the child stops instead of the worker crashing.

    Args:
        tmp_path: Pytest's temporary folder.
    """
    requests = _requests(tmp_path, lambda candidate, case: (float("nan"), {}))
    replies = io.StringIO()

    sent = requests.line(
        'APP_NATIVE_RPC n {"id": "0", "kind": "evaluate", "candidate": "seed", '
        '"example": {"id": 0}, "candidate_id": 3}',
        replies,
    )

    assert sent is True
    assert json.loads(replies.getvalue()) == {"id": "0", "error": "The worker returned an invalid reply."}


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
        requests._evaluate({"candidate": candidate, "example": {"id": 0}})
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

        nonce = json.load(open(sys.argv[1]))["nonce"]


        def emit(prefix, payload):
            print(f"{prefix}{nonce} {json.dumps(payload)}", flush=True)
    """
    script.write_text(textwrap.dedent(head) + textwrap.dedent(body), encoding="utf-8")
    python = tmp_path / "shinka-python"
    # The engine starts ``-c <limit program> <limits> <runner> <input>``. The
    # limit program still runs, and then starts the script in the runner's place.
    python.write_text(f'#!/bin/sh\nexec {sys.executable} "$1" "$2" "$3" {script} "$5"\n', encoding="utf-8")
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
    return EngineContext(
        reflection_lm=lambda prompt: "",
        run_dir=str(tmp_path),
        call_model=lambda *args: None,  # type: ignore[arg-type]
    )


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
            # Finite, so a job the stop misses fails the test instead of hanging it.
            [sys.executable, "-c", "import time; time.sleep(20)"],
            stdout=subprocess.DEVNULL,
            stderr=subprocess.DEVNULL,
        )
        open({str(job_pid)!r}, "w").write(str(job.pid))
        emit("APP_NATIVE_RPC ", {{"kind": "evaluate", "id": "0"}})
        # Shorter than the job's sleep: a job that survives the stop is still
        # alive when this child ends on its own.
        time.sleep(5)
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

    The eval server scored the version before the stop: the engine takes a
    version's score only from the server's own records.

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
    server = EvalServer(lambda candidate, case: (3.0, {}), max_evals=5)
    server.evaluate("late", None)

    with pytest.raises(BudgetReached) as stopped:
        ShinkaEvolveEngine(python).run(Task("seed"), server, _context(tmp_path))

    assert stopped.value.result is not None
    assert (stopped.value.result.best_candidate, stopped.value.result.best_score) == ("late", 3.0)


def _wait_until_dead(pid: int) -> bool:
    """Wait up to ten seconds for a process to end.

    Args:
        pid: The process id.

    Returns:
        Whether the process ended in time.
    """
    deadline = time.monotonic() + 10
    while time.monotonic() < deadline:
        try:
            os.kill(pid, 0)
        except ProcessLookupError:
            return True
        time.sleep(0.05)
    return False


def _idle_limits(monkeypatch: pytest.MonkeyPatch, start: float, after: list[float]) -> list[str]:
    """Set the idle limit of the child's start, and a new idle limit after each line the worker handles.

    The engine arms the limit from ``_IDLE_SECONDS`` at the start and again after
    each line. A loaded test host can slow the child's start past a short limit,
    so a test sets its short limit only after the line where it starts to count.

    Args:
        monkeypatch: Sets the limit and wraps ``_Requests.line``.
        start: The limit before the child's first line.
        after: The limit after the first, second and later lines; the last one repeats.

    Returns:
        The lines the worker handled, in order.
    """
    monkeypatch.setattr(shinka_evolve, "_IDLE_SECONDS", start)
    handled: list[str] = []
    line = shinka_evolve._Requests.line

    def counted(self: shinka_evolve._Requests, text: str, replies: io.StringIO | None) -> bool:
        """Handle the line, then set the limit the engine arms next.

        Args:
            self: The request handler.
            text: The line.
            replies: The child's stdin, or None in the drain.

        Returns:
            Whether a reply went to the child.
        """
        try:
            return line(self, text, replies)
        finally:
            handled.append(text)
            monkeypatch.setattr(shinka_evolve, "_IDLE_SECONDS", after[min(len(handled), len(after)) - 1])

    monkeypatch.setattr(shinka_evolve._Requests, "line", counted)
    return handled


def test_the_child_gets_the_memory_and_cpu_limits(monkeypatch: pytest.MonkeyPatch) -> None:
    """Set the address-space and CPU limits, since the child parses untrusted model replies.

    Args:
        monkeypatch: Turns the memory limit on.
    """
    monkeypatch.setattr(shinka_evolve, "_MEMORY_LIMITED", True)

    assert shinka_evolve._child_limits() == {
        "RLIMIT_AS": shinka_evolve._CHILD_MEMORY_BYTES,
        "RLIMIT_CPU": shinka_evolve._CHILD_CPU_SECONDS,
        "RLIMIT_FSIZE": shinka_evolve._CHILD_FILE_BYTES,
    }


def test_the_child_starts_with_no_preexec_fn(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    """Start the child with no ``preexec_fn``, which is not safe in the threaded worker.

    Args:
        tmp_path: Pytest's temporary folder.
        monkeypatch: Replaces the child process.
    """
    _, children = _fake_popen(monkeypatch, "")

    ShinkaEvolveEngine("python")._drive(_requests(tmp_path, lambda candidate, case: (1.0, {})), tmp_path)

    assert children[0].popen.get("preexec_fn") is None


def test_the_real_child_runs_under_the_memory_and_cpu_limits(tmp_path: Path) -> None:
    """Read the limits inside the real child, so the limits reach the process that parses model replies.

    Only Linux lowers the address-space limit; elsewhere the child keeps the worker's.

    Args:
        tmp_path: Pytest's temporary folder.
    """
    limits = tmp_path / "limits.json"
    python = _scripted_child(
        tmp_path,
        f"""
        import resource

        open({str(limits)!r}, "w").write(
            json.dumps(
                [
                    resource.getrlimit(resource.RLIMIT_AS),
                    resource.getrlimit(resource.RLIMIT_CPU),
                    resource.getrlimit(resource.RLIMIT_FSIZE),
                ]
            )
        )
        """,
    )

    with pytest.raises(ServiceError, match="ShinkaEvolve failed"):
        ShinkaEvolveEngine(python).run(
            Task("seed"), EvalServer(lambda candidate, case: (1.0, {}), max_evals=5), _context(tmp_path)
        )

    memory, cpu, size = (
        shinka_evolve._CHILD_MEMORY_BYTES,
        shinka_evolve._CHILD_CPU_SECONDS,
        shinka_evolve._CHILD_FILE_BYTES,
    )
    address_space = [memory, memory] if shinka_evolve._MEMORY_LIMITED else list(resource.getrlimit(resource.RLIMIT_AS))
    assert json.loads(limits.read_text()) == [address_space, [cpu, cpu], [size, size]]


def test_a_child_past_its_cpu_limit_is_killed(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    """Kill a child that spins past its CPU limit, so a reply that makes a parser loop cannot hold the host.

    Args:
        tmp_path: Pytest's temporary folder.
        monkeypatch: Sets a one-second CPU limit.
    """
    python = _scripted_child(
        tmp_path,
        """
        end = time.monotonic() + 60
        while time.monotonic() < end:
            pass
        """,
    )
    monkeypatch.setattr(shinka_evolve, "_CHILD_CPU_SECONDS", 1)
    started = time.monotonic()

    with pytest.raises(ServiceError, match="ShinkaEvolve failed"):
        ShinkaEvolveEngine(python).run(
            Task("seed"), EvalServer(lambda candidate, case: (1.0, {}), max_evals=5), _context(tmp_path)
        )

    assert time.monotonic() - started < 30


@pytest.mark.parametrize(
    ("before", "start", "after"),
    [
        # The start limit is the one under test, so it is long enough for a loaded host to start the child.
        ("", 5.0, []),
        (
            """
        emit("APP_NATIVE_RPC ", {"kind": "evaluate", "id": "0", "candidate": "x"})
        sys.stdin.readline()
        """,
            30.0,
            [0.5],
        ),
        (
            """
        emit("APP_NATIVE_RPC ", {"kind": "evaluate", "id": "0", "candidate": "x"})
        sys.stdin.readline()
        emit("APP_NATIVE_PROGRESS ", {"event": "case_scored", "candidate_id": 0, "score": 1.0, "total": 1})
        emit("APP_NATIVE_LOG ", {"message": "scored"})
        """,
            30.0,
            [30.0, 30.0, 0.5],
        ),
    ],
    ids=["before-its-first-line", "after-a-reply", "after-a-progress-line"],
)
def test_a_child_that_goes_quiet_is_killed(
    before: str, start: float, after: list[float], tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Kill the child's group and fail the run when the child writes nothing for the idle limit.

    The short limit starts only after the child's last line, so the kill comes after that line.

    Args:
        before: What the child does before it goes quiet.
        start: The idle limit before the child's first line.
        after: The idle limit after each line.
        tmp_path: Pytest's temporary folder.
        monkeypatch: Sets the idle limits.
    """
    child_pid = tmp_path / "child.pid"
    python = _scripted_child(
        tmp_path,
        f"""
        import os

        open({str(child_pid)!r}, "w").write(str(os.getpid()))
        """
        + before
        + """
        # Longer than the time limit below, so a missed kill fails the test, not hangs it.
        time.sleep(30)
        """,
    )
    handled = _idle_limits(monkeypatch, start, after)
    started = time.monotonic()

    with pytest.raises(ServiceError, match="wrote nothing"):
        ShinkaEvolveEngine(python).run(
            Task("seed"), EvalServer(lambda candidate, case: (1.0, {}), max_evals=5), _context(tmp_path)
        )

    assert time.monotonic() - started < 15
    assert len(handled) == before.count("emit(")
    assert _wait_until_dead(int(child_pid.read_text()))


def test_the_idle_limit_does_not_run_while_the_worker_answers(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    """Start the idle limit only once the reply is sent, so a slow scorer or model never kills the child.

    Args:
        tmp_path: Pytest's temporary folder.
        monkeypatch: Sets the idle limits.
    """
    python = _scripted_child(
        tmp_path,
        """
        # One write, so the request is already in the pipe when the short limit starts.
        log = json.dumps({"message": "ready"})
        rpc = json.dumps({"kind": "evaluate", "id": "0", "candidate": "x"})
        print(f"APP_NATIVE_LOG {nonce} {log}\\nAPP_NATIVE_RPC {nonce} {rpc}", flush=True)
        sys.stdin.readline()
        emit("APP_NATIVE_PROGRESS ", {"event": "candidate", "candidate_id": 0, "candidate": "x", "score": 1.0})
        """,
    )
    # The short limit runs from the ready line to the request: it is the limit
    # that a timer left running while the worker answers would hit.
    handled = _idle_limits(monkeypatch, 30.0, [1.5, 30.0])

    def slow(candidate: object, case: object) -> tuple[float, dict[str, object]]:
        """Score after a delay longer than the idle limit.

        Args:
            candidate: The version, ignored.
            case: The case, ignored.

        Returns:
            A score of one and no side information.
        """
        time.sleep(4.0)
        return 1.0, {}

    result = ShinkaEvolveEngine(python).run(Task("seed"), EvalServer(slow, max_evals=5), _context(tmp_path))

    assert (result.best_candidate, result.best_score) == ("x", 1.0)
    assert len(handled) == 3


def test_a_stopped_child_whose_group_holds_the_pipe_is_killed_after_the_drain_limit(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Kill the group when a member keeps the output open past the drain limit after a budget stop.

    Args:
        tmp_path: Pytest's temporary folder.
        monkeypatch: Stops the run at the child's first request and sets a short drain limit.
    """
    holder_pid = tmp_path / "holder.pid"
    python = _scripted_child(
        tmp_path,
        f"""
        # It keeps the child's output open and ignores the closed reply stream.
        # Longer than the time limit below, so a missed drain limit fails the test, not hangs it.
        holder = subprocess.Popen([sys.executable, "-c", "import time; time.sleep(20)"], stdin=subprocess.DEVNULL)
        open({str(holder_pid)!r}, "w").write(str(holder.pid))
        emit("APP_NATIVE_RPC ", {{"kind": "evaluate", "id": "0"}})
        sys.stdin.read()
        sys.exit(1)
        """,
    )
    monkeypatch.setattr(shinka_evolve._Requests, "_request", _stopping_request(BudgetReached()))
    monkeypatch.setattr(shinka_evolve, "_DRAIN_SECONDS", 0.5)
    started = time.monotonic()
    pid = 0
    try:
        with pytest.raises(BudgetReached):
            ShinkaEvolveEngine(python).run(
                Task("seed"), EvalServer(lambda candidate, case: (1.0, {}), max_evals=5), _context(tmp_path)
            )
        pid = int(holder_pid.read_text())
        assert time.monotonic() - started < 10
        assert _wait_until_dead(pid)
    finally:
        if pid:
            with contextlib.suppress(ProcessLookupError):
                os.kill(pid, signal.SIGKILL)


class _FakeChild:
    """Stand in for the child process and record the calls that end it."""

    def __init__(self, calls: list[str], output: str, **popen: object) -> None:
        """Keep the call record and the output the child writes.

        Args:
            calls: The shared record of ``wait`` and ``killpg`` calls.
            output: The child's whole output.
            **popen: ``Popen``'s keyword arguments.
        """
        self.calls = calls
        self.popen = popen
        self.pid = 4242
        self.stdin = io.StringIO()
        self.stdout = io.StringIO(output)

    def wait(self) -> int:
        """Reap the child.

        Returns:
            Exit code zero.
        """
        self.calls.append("wait")
        return 0


def _fake_popen(monkeypatch: pytest.MonkeyPatch, output: str) -> tuple[list[str], list[_FakeChild]]:
    """Replace the child process and the group kill with recorders.

    Args:
        monkeypatch: Replaces ``subprocess.Popen`` and ``os.killpg``.
        output: The child's whole output.

    Returns:
        The record of ``wait`` and ``killpg`` calls, and the children started.
    """
    calls: list[str] = []
    children: list[_FakeChild] = []

    def popen(command: list[str], **kwargs: object) -> _FakeChild:
        """Start a fake child.

        Args:
            command: The child's command, ignored.
            **kwargs: ``Popen``'s keyword arguments.

        Returns:
            The fake child.
        """
        children.append(_FakeChild(calls, output, **kwargs))
        return children[-1]

    monkeypatch.setattr(shinka_evolve.subprocess, "Popen", popen)
    monkeypatch.setattr(shinka_evolve.os, "killpg", lambda pid, sig: calls.append("killpg"))
    return calls, children


@pytest.mark.parametrize("stopped", [False, True], ids=["finished", "budget-stop"])
def test_the_group_is_killed_before_the_leader_is_reaped(
    stopped: bool, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Kill the group while the leader is unreaped, so its id cannot belong to an unrelated process.

    Args:
        stopped: Whether a budget stop ends the run, so the child drains first.
        tmp_path: Pytest's temporary folder.
        monkeypatch: Replaces the child process and stops the run at its first request.
    """
    calls, _ = _fake_popen(monkeypatch, 'APP_NATIVE_RPC n {"kind": "evaluate", "id": "0"}\n' if stopped else "")
    monkeypatch.setattr(shinka_evolve._Requests, "_request", _stopping_request(BudgetReached()))
    requests = _requests(tmp_path, lambda candidate, case: (1.0, {}))

    with pytest.raises(BudgetReached) if stopped else contextlib.nullcontext():
        ShinkaEvolveEngine("python")._drive(requests, tmp_path)

    assert calls == ["killpg", "wait"]


def test_the_child_hashes_strings_with_a_fixed_seed(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    """Fix the child's string hashing, so set order, and the run's steps, repeat on a resume.

    Args:
        tmp_path: Pytest's temporary folder.
        monkeypatch: Replaces the child process.
    """
    _, children = _fake_popen(monkeypatch, "")

    ShinkaEvolveEngine("python")._drive(_requests(tmp_path, lambda candidate, case: (1.0, {})), tmp_path)

    env = children[0].popen["env"]
    assert isinstance(env, dict)
    assert env["PYTHONHASHSEED"] == "0"


def test_the_child_runs_one_math_thread_and_few_malloc_arenas(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    """Keep thread buffers and malloc arenas from filling the child's address-space limit.

    Args:
        tmp_path: Pytest's temporary folder.
        monkeypatch: Replaces the child process.
    """
    _, children = _fake_popen(monkeypatch, "")

    ShinkaEvolveEngine("python")._drive(_requests(tmp_path, lambda candidate, case: (1.0, {})), tmp_path)

    env = children[0].popen["env"]
    assert isinstance(env, dict)
    assert (env["OPENBLAS_NUM_THREADS"], env["OMP_NUM_THREADS"], env["MALLOC_ARENA_MAX"]) == ("1", "1", "2")


@pytest.mark.parametrize(
    "fields",
    [
        {"per_example": "ab"},
        {"per_example": [["0", "bad"]]},
        {"per_example": [["0"]]},
        {"per_example": [["0", float("nan")]]},
        {"total_evals": "many"},
        {"total_evals": True},
        {"candidate": ["good"]},
        {"candidate": {"part": "good"}},
        {"score": float("nan")},
        {"score": "2"},
    ],
    ids=[
        "per-example-text",
        "per-example-text-score",
        "per-example-no-pair",
        "per-example-nan",
        "total-evals-text",
        "total-evals-bool",
        "candidate-list",
        "candidate-wrong-shape",
        "score-nan",
        "score-text",
    ],
)
def test_a_malformed_scored_version_is_ignored(
    fields: dict[str, object], tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Ignore a progress line with a malformed field, so child output can neither fail the run nor be its result.

    Args:
        fields: The malformed fields of the progress line.
        tmp_path: Pytest's temporary folder.
        monkeypatch: Records the shown versions.
    """
    shown: list[object] = []
    monkeypatch.setattr(shinka_evolve, "emit_candidate", lambda callback, **kwargs: shown.append(kwargs))
    requests = _scored_requests(tmp_path)
    event = {"event": "candidate", "candidate_id": 1, "candidate": "good", "score": 2.0, "total_evals": 1, **fields}

    requests.line("APP_NATIVE_PROGRESS n " + json.dumps(event), io.StringIO())

    assert shown == []
    assert requests.best is None


def test_a_scored_case_with_a_score_that_is_not_finite_is_ignored(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Ignore a case score that is not a finite number.

    Args:
        tmp_path: Pytest's temporary folder.
        monkeypatch: Records the shown case scores.
    """
    shown: list[object] = []
    monkeypatch.setattr(shinka_evolve, "emit_case_scored", lambda callback, **kwargs: shown.append(kwargs))
    requests = _scored_requests(tmp_path)
    event = {"event": "case_scored", "candidate_id": 1, "example_id": "0", "score": float("inf"), "total": 1}

    requests.line("APP_NATIVE_PROGRESS n " + json.dumps(event), io.StringIO())

    assert shown == []


def test_the_best_version_takes_its_score_from_the_eval_server(tmp_path: Path) -> None:
    """Keep the server's score of a version, and never a version the server did not score.

    Args:
        tmp_path: Pytest's temporary folder.
    """
    requests = _scored_requests(tmp_path)

    _report(requests, "never-scored", 9.0, 1)
    assert requests.best is None
    _report(requests, "good", 9.0, 2)

    assert requests.best == {"candidate": "good", "score": 2.0}


@pytest.mark.parametrize(
    "document",
    [
        {"error": "upstream broke", "interrupted_incumbent": {"best_candidate": ["good"], "best_score": 3.0}},
        {"error": "upstream broke", "interrupted_incumbent": {"best_candidate": "never-scored", "best_score": 3.0}},
        {"error": "upstream broke", "interrupted_incumbent": "good", "metadata": ["x"]},
        {"best_candidate": ["good"], "best_score": 2.0},
        {"best_candidate": "never-scored", "best_score": 2.0},
        ["good"],
    ],
    ids=[
        "incumbent-list",
        "incumbent-never-scored",
        "incumbent-not-a-mapping",
        "result-list",
        "result-never-scored",
        "document-list",
    ],
)
def test_a_result_with_no_version_the_server_scored_fails_the_run(document: object, tmp_path: Path) -> None:
    """Fail the run, with no crash, when the child's result names no version the eval server scored.

    Args:
        document: The child's result document.
        tmp_path: Pytest's temporary folder.
    """
    workdir = tmp_path / "child"
    workdir.mkdir()
    (workdir / "stderr.log").write_text("")
    (workdir / "native_result.json").write_text(json.dumps(document))

    with pytest.raises(ServiceError, match="ShinkaEvolve failed"):
        ShinkaEvolveEngine._result(workdir, 0, _scored_requests(tmp_path))


def test_a_result_takes_its_score_and_evaluations_from_the_worker(tmp_path: Path) -> None:
    """Report the eval server's score and the worker's evaluation count, not the child's claims.

    Args:
        tmp_path: Pytest's temporary folder.
    """
    workdir = tmp_path / "child"
    _write_result(workdir, {"best_candidate": "good", "best_score": 9.0, "total_evals": "many", "metadata": {"k": 1}})
    requests = _scored_requests(tmp_path)
    requests.evals = 2

    result = ShinkaEvolveEngine._result(workdir, 0, requests)

    assert (result.best_candidate, result.best_score, result.total_evals) == ("good", 2.0, 2)
    assert result.metadata["k"] == 1


@pytest.mark.parametrize(
    "seed_answer",
    [(None, {}), (None, {"status": "run_failed", "feedback": "Run failed: boom"})],
    ids=["metric-error", "run-failed"],
)
def test_a_run_where_every_version_fails_finishes_on_the_seed_with_no_score(
    seed_answer: tuple[None, dict[str, str]], tmp_path: Path
) -> None:
    """Keep the seed with no score when the child ends cleanly on it after every version failed (SKY-751).

    Args:
        seed_answer: The scorer's answer for the seed.
        tmp_path: Pytest's temporary folder.
    """
    requests = _requests(tmp_path, lambda candidate, case: seed_answer)
    requests._evaluate({"candidate": "seed", "example": {"id": 0}})
    workdir = tmp_path / "child"
    _write_result(workdir, {"best_candidate": "seed", "best_score": None, "metadata": {"k": 1}})

    result = ShinkaEvolveEngine._result(workdir, 0, requests)

    assert (result.best_candidate, result.best_score, result.total_evals) == ("seed", None, 1)
    assert result.metadata["k"] == 1


@pytest.mark.parametrize(
    ("seed", "returncode", "document"),
    [
        ("seed", 0, {"best_candidate": "bad", "best_score": None}),
        ("seed", 1, {"best_candidate": "seed", "best_score": None}),
        ("seed", 0, {"best_candidate": "seed", "best_score": None, "error": "upstream broke"}),
        ("seed", 0, {"best_candidate": "seed", "best_score": 0.5}),
        (None, 0, {"best_score": None}),
    ],
    ids=["not-the-seed", "child-failed", "child-reported-an-error", "child-claims-a-score", "task-with-no-seed"],
)
def test_a_result_with_no_score_fails_the_run_unless_it_is_a_clean_end_on_the_seed(
    seed: str | None, returncode: int, document: dict[str, object], tmp_path: Path
) -> None:
    """Fail the run on a version with no score that is not the seed, or on a child that failed.

    A claimed score for a seed the eval server holds no score for is also a
    failure, and so is a result that names no version in a task with no seed.

    Args:
        seed: The task's seed.
        returncode: The child's exit code.
        document: The child's result document.
        tmp_path: Pytest's temporary folder.
    """
    requests = _requests(tmp_path, lambda candidate, case: (None, {}))
    requests.task = replace(requests.task, seed_candidate=seed)
    for candidate in ("seed", "bad"):
        requests._evaluate({"candidate": candidate, "example": {"id": 0}})
    workdir = tmp_path / "child"
    _write_result(workdir, document)

    with pytest.raises(ServiceError, match="ShinkaEvolve failed"):
        ShinkaEvolveEngine._result(workdir, returncode, requests)


@pytest.mark.parametrize("score", [float("nan"), float("inf")], ids=["nan", "inf"])
def test_a_version_with_a_score_that_is_not_finite_is_never_the_best_or_the_result(
    score: float, tmp_path: Path
) -> None:
    """Keep no best version and fail the run when the scorer gives a score that is not finite.

    Args:
        score: The scorer's score for every case.
        tmp_path: Pytest's temporary folder.
    """
    requests = _requests(tmp_path, lambda candidate, case: (score, {}))
    requests._evaluate({"candidate": "good", "example": {"id": 0}})
    _report(requests, "good", 1.0, 1)
    workdir = tmp_path / "child"
    _write_result(workdir, {"best_candidate": "good", "best_score": 1.0})

    assert requests.best is None
    with pytest.raises(ServiceError, match="ShinkaEvolve failed"):
        ShinkaEvolveEngine._result(workdir, 0, requests)


def test_a_partial_result_document_keeps_the_best_reported_version(tmp_path: Path) -> None:
    """Treat a result document cut off in its write as no document.

    Args:
        tmp_path: Pytest's temporary folder.
    """
    workdir = tmp_path / "child"
    _write_result(workdir, None)
    (workdir / "native_result.json").write_text('{"best_candidate": "go')
    requests = _scored_requests(tmp_path)
    _report(requests, "good", 2.0, 1)

    result = ShinkaEvolveEngine._result(workdir, -9, requests)

    assert (result.best_candidate, result.best_score) == ("good", 2.0)


def test_a_failure_message_holds_only_the_end_of_the_stderr_log(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Read only the end of the stderr log, which the child can make large.

    Args:
        tmp_path: Pytest's temporary folder.
        monkeypatch: Sets a short tail.
    """
    workdir = tmp_path / "child"
    _write_result(workdir, None)
    (workdir / "stderr.log").write_text("HEAD" + "x" * 20 + "the-real-error")
    monkeypatch.setattr(shinka_evolve, "_STDERR_TAIL_BYTES", 20)

    with pytest.raises(ServiceError) as failed:
        ShinkaEvolveEngine._result(workdir, 1, _scored_requests(tmp_path))

    assert str(failed.value) == "ShinkaEvolve failed: " + "x" * 6 + "the-real-error"


def test_a_malformed_progress_line_in_the_drain_keeps_the_budget_stop(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Raise the budget stop, not a parse error, when the child reports a malformed version while it drains.

    Args:
        tmp_path: Pytest's temporary folder.
        monkeypatch: Stops the run at the child's first request.
    """
    python = _scripted_child(
        tmp_path,
        """
        emit("APP_NATIVE_RPC ", {"kind": "evaluate", "id": "0"})
        sys.stdin.read()
        emit("APP_NATIVE_PROGRESS ", {"event": "candidate", "candidate_id": 1, "candidate": ["x"], "score": 3.0})
        emit("APP_NATIVE_PROGRESS ", {"event": "candidate", "candidate_id": 2, "candidate": "y", "score": 3.0,
                                      "per_example": "ab", "total_evals": "many"})
        sys.exit(1)
        """,
    )
    monkeypatch.setattr(shinka_evolve._Requests, "_request", _stopping_request(BudgetReached()))

    with pytest.raises(BudgetReached) as stopped:
        ShinkaEvolveEngine(python).run(
            Task("seed"), EvalServer(lambda candidate, case: (1.0, {}), max_evals=5), _context(tmp_path)
        )

    assert stopped.value.result is None


# Indented as the scripted bodies around it, which are dedented as one block.
_SCORE_ONE_VERSION = """
        emit("APP_NATIVE_RPC ", {"kind": "evaluate", "id": "0", "candidate": "x"})
        sys.stdin.readline()
        emit("APP_NATIVE_PROGRESS ", {"event": "candidate", "candidate_id": 0, "candidate": "x", "score": 1.0})
"""


def test_a_child_that_goes_quiet_after_a_scored_version_returns_that_version(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Keep the best scored version when the worker stops a child that went quiet.

    Args:
        tmp_path: Pytest's temporary folder.
        monkeypatch: Sets the idle limits.
    """
    child_pid = tmp_path / "child.pid"
    python = _scripted_child(
        tmp_path,
        f"""
        import os

        open({str(child_pid)!r}, "w").write(str(os.getpid()))
        """
        + _SCORE_ONE_VERSION
        + """
        # Longer than the time limit below, so a missed kill fails the test, not hangs it.
        time.sleep(30)
        """,
    )
    # The short limit starts at the scored version, so the kill comes after it.
    handled = _idle_limits(monkeypatch, 30.0, [30.0, 1.0])
    started = time.monotonic()

    result = ShinkaEvolveEngine(python).run(
        Task("seed"), EvalServer(lambda candidate, case: (1.0, {}), max_evals=5), _context(tmp_path)
    )

    assert (result.best_candidate, result.best_score) == ("x", 1.0)
    assert len(handled) == 2
    assert time.monotonic() - started < 15
    assert _wait_until_dead(int(child_pid.read_text()))


@pytest.mark.parametrize("scored", [False, True], ids=["before-a-scored-version", "after-a-scored-version"])
def test_a_child_that_writes_an_overlong_line_is_stopped(
    scored: bool, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Stop reading at the line limit, kill the child, and keep the best scored version if there is one.

    Args:
        scored: Whether the child reports a scored version before the long line.
        tmp_path: Pytest's temporary folder.
        monkeypatch: Sets a short line limit.
    """
    child_pid = tmp_path / "child.pid"
    python = _scripted_child(
        tmp_path,
        f"""
        import os

        open({str(child_pid)!r}, "w").write(str(os.getpid()))
        """
        + (_SCORE_ONE_VERSION if scored else "")
        + """
        print("y" * 1000, flush=True)
        # Longer than the time limit below, so a missed kill fails the test, not hangs it.
        time.sleep(20)
        """,
    )
    monkeypatch.setattr(shinka_evolve, "_LINE_CHARS", 300)
    server = EvalServer(lambda candidate, case: (1.0, {}), max_evals=5)
    started = time.monotonic()

    if scored:
        result = ShinkaEvolveEngine(python).run(Task("seed"), server, _context(tmp_path))
        assert (result.best_candidate, result.best_score) == ("x", 1.0)
    else:
        with pytest.raises(ServiceError, match="longer than 300 characters"):
            ShinkaEvolveEngine(python).run(Task("seed"), server, _context(tmp_path))

    assert time.monotonic() - started < 10
    assert _wait_until_dead(int(child_pid.read_text()))


def test_a_line_at_the_limit_with_its_newline_is_read(monkeypatch: pytest.MonkeyPatch) -> None:
    """Read a line whose newline falls within the limit, and refuse one character more.

    Args:
        monkeypatch: Sets a short line limit.
    """
    monkeypatch.setattr(shinka_evolve, "_LINE_CHARS", 4)

    assert shinka_evolve._read_line(io.StringIO("abc\nabc")) == "abc"
    assert shinka_evolve._read_line(io.StringIO("abc")) == "abc"
    assert shinka_evolve._read_line(io.StringIO("")) is None
    with pytest.raises(shinka_evolve._OverlongLineError):
        shinka_evolve._read_line(io.StringIO("abcd\n"))


def test_an_overlong_line_in_the_drain_keeps_the_budget_stop(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    """End the drain at an overlong line, and raise the budget stop with the version reported before it.

    Args:
        tmp_path: Pytest's temporary folder.
        monkeypatch: Stops the run at the child's first request and sets a short line limit.
    """
    python = _scripted_child(
        tmp_path,
        """
        emit("APP_NATIVE_RPC ", {"kind": "evaluate", "id": "0"})
        sys.stdin.read()
        emit("APP_NATIVE_PROGRESS ", {"event": "candidate", "candidate_id": 1, "candidate": "late", "score": 3.0})
        print("y" * 1000, flush=True)
        sys.exit(1)
        """,
    )
    monkeypatch.setattr(shinka_evolve._Requests, "_request", _stopping_request(BudgetReached()))
    monkeypatch.setattr(shinka_evolve, "_LINE_CHARS", 300)
    server = EvalServer(lambda candidate, case: (3.0, {}), max_evals=5)
    server.evaluate("late", None)

    with pytest.raises(BudgetReached) as stopped:
        ShinkaEvolveEngine(python).run(Task("seed"), server, _context(tmp_path))

    assert stopped.value.result is not None
    assert (stopped.value.result.best_candidate, stopped.value.result.best_score) == ("late", 3.0)


# Deep enough that ``json.loads`` raises ``RecursionError``, not ``ValueError``.
_DEEP = "[" * 100_000


@pytest.mark.parametrize("prefix", ["APP_NATIVE_LOG ", "APP_NATIVE_PROGRESS "], ids=["log", "progress"])
def test_a_deeply_nested_log_or_progress_line_does_not_stop_the_run(
    prefix: str, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Keep a deeply nested log or progress line as plain output, and go on to the child's next lines.

    Args:
        prefix: The frame of the nested line.
        tmp_path: Pytest's temporary folder.
        monkeypatch: Records the plain output lines.
    """
    plain: list[str] = []
    monkeypatch.setattr(sandbox_log, "stream_line", lambda line, **kwargs: plain.append(line))
    python = _scripted_child(
        tmp_path,
        f"""
        print(f"{prefix}{{nonce}} {_DEEP}", flush=True)
        """
        + _SCORE_ONE_VERSION,
    )

    result = ShinkaEvolveEngine(python).run(
        Task("seed"), EvalServer(lambda candidate, case: (1.0, {}), max_evals=5), _context(tmp_path)
    )

    assert (result.best_candidate, result.best_score) == ("x", 1.0)
    assert plain == [_DEEP]


def test_a_deeply_nested_request_is_a_malformed_request(tmp_path: Path) -> None:
    """Refuse a deeply nested request as malformed, not with the parser's ``RecursionError``.

    Args:
        tmp_path: Pytest's temporary folder.
    """
    requests = _requests(tmp_path, lambda candidate, case: (1.0, {}))

    with pytest.raises(ServiceError, match="malformed request"):
        requests.line(f"APP_NATIVE_RPC n {_DEEP}", io.StringIO())


def _fail_to_show(callback: object, **fields: object) -> None:
    """Fail as a broken progress callback does.

    Args:
        callback: The run's progress callback, ignored.
        **fields: The version's fields, ignored.

    Raises:
        RuntimeError: Always.
    """
    raise RuntimeError("the progress callback broke")


@pytest.mark.parametrize("broken", ["deep-line", "progress-error"])
def test_a_line_the_drain_cannot_read_keeps_the_budget_stop(
    broken: str, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Raise the budget stop with its best version when a line in the drain fails to parse or to show.

    Args:
        broken: A deeply nested progress line, or a progress callback that fails.
        tmp_path: Pytest's temporary folder.
        monkeypatch: Stops the run at the child's first request, and breaks the progress callback.
    """
    deep = f'print(f"APP_NATIVE_PROGRESS {{nonce}} {_DEEP}", flush=True)' if broken == "deep-line" else ""
    python = _scripted_child(
        tmp_path,
        f"""
        emit("APP_NATIVE_RPC ", {{"kind": "evaluate", "id": "0"}})
        sys.stdin.read()
        {deep}
        emit("APP_NATIVE_PROGRESS ", {{"event": "candidate", "candidate_id": 1, "candidate": "late", "score": 3.0}})
        sys.exit(1)
        """,
    )
    monkeypatch.setattr(shinka_evolve._Requests, "_request", _stopping_request(BudgetReached()))
    if broken == "progress-error":
        monkeypatch.setattr(shinka_evolve, "emit_candidate", _fail_to_show)
    server = EvalServer(lambda candidate, case: (3.0, {}), max_evals=5)
    server.evaluate("late", None)

    with pytest.raises(BudgetReached) as stopped:
        ShinkaEvolveEngine(python).run(Task("seed"), server, _context(tmp_path))

    assert stopped.value.result is not None
    assert (stopped.value.result.best_candidate, stopped.value.result.best_score) == ("late", 3.0)


# Raw bytes that are not UTF-8, as upstream writes when it prints a model text with a lone surrogate.
_INVALID_UTF8_LINE = """
        sys.stdout.flush()
        sys.stdout.buffer.write(b"\\xff\\n")
        sys.stdout.buffer.flush()
"""


def test_a_line_that_is_not_utf8_does_not_stop_the_run(tmp_path: Path) -> None:
    """Read past a line with an invalid byte, and go on to the child's next lines.

    Args:
        tmp_path: Pytest's temporary folder.
    """
    python = _scripted_child(tmp_path, _INVALID_UTF8_LINE + _SCORE_ONE_VERSION)

    result = ShinkaEvolveEngine(python).run(
        Task("seed"), EvalServer(lambda candidate, case: (1.0, {}), max_evals=5), _context(tmp_path)
    )

    assert (result.best_candidate, result.best_score) == ("x", 1.0)


def test_a_line_that_is_not_utf8_in_the_drain_keeps_the_budget_stop(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Raise the budget stop with its best version when the child writes an invalid byte while it drains.

    Args:
        tmp_path: Pytest's temporary folder.
        monkeypatch: Stops the run at the child's first request.
    """
    python = _scripted_child(
        tmp_path,
        """
        emit("APP_NATIVE_RPC ", {"kind": "evaluate", "id": "0"})
        sys.stdin.read()
        """
        + _INVALID_UTF8_LINE
        + """
        emit("APP_NATIVE_PROGRESS ", {"event": "candidate", "candidate_id": 1, "candidate": "late", "score": 3.0})
        sys.exit(1)
        """,
    )
    monkeypatch.setattr(shinka_evolve._Requests, "_request", _stopping_request(BudgetReached()))
    server = EvalServer(lambda candidate, case: (3.0, {}), max_evals=5)
    server.evaluate("late", None)

    with pytest.raises(BudgetReached) as stopped:
        ShinkaEvolveEngine(python).run(Task("seed"), server, _context(tmp_path))

    assert stopped.value.result is not None
    assert (stopped.value.result.best_candidate, stopped.value.result.best_score) == ("late", 3.0)


def _two_case_requests(tmp_path: Path, scores: dict[str, float]) -> shinka_evolve._Requests:
    """Build a request handler for a task with two cases.

    Args:
        tmp_path: Pytest's temporary folder.
        scores: Each version's score on every case.

    Returns:
        A handler for the nonce ``n``.
    """
    task = Task("seed", cases=[{"input": "a"}, {"input": "b"}])
    ctx = EngineContext(reflection_lm=lambda prompt: "", run_dir=str(tmp_path), call_model=None)
    server = EvalServer(lambda candidate, case: (scores[candidate], {}), max_evals=10)
    return shinka_evolve._Requests(task, server, ctx, "n")


def _evaluate_line(candidate: str, example: dict[str, int] | None) -> str:
    """Frame one evaluation request line of the child, for the nonce ``n``.

    Args:
        candidate: The version.
        example: The case the child names, or None for no case.

    Returns:
        The framed line.
    """
    request = {"id": "0", "kind": "evaluate", "candidate": candidate, "example": example, "candidate_id": 1}
    return "APP_NATIVE_RPC n " + json.dumps(request)


def test_a_version_scored_on_only_some_cases_is_never_the_best(tmp_path: Path) -> None:
    """Keep a version scored on every case over a higher version scored on only some, whose mean is partial.

    Args:
        tmp_path: Pytest's temporary folder.
    """
    requests = _two_case_requests(tmp_path, {"x": 1.0, "y": 0.5})
    requests.line(_evaluate_line("x", {"id": 0}), io.StringIO())
    for index in (0, 1):
        requests.line(_evaluate_line("y", {"id": index}), io.StringIO())

    _report(requests, "x", 1.0, 1)
    _report(requests, "y", 0.5, 2)

    assert requests.best == {"candidate": "y", "score": 0.5}
    assert requests.recorded_score("x") is None


def test_of_two_versions_with_the_same_full_score_the_first_stays_the_best(tmp_path: Path) -> None:
    """Keep the first version scored on every case when a later one only ties its score.

    Args:
        tmp_path: Pytest's temporary folder.
    """
    requests = _two_case_requests(tmp_path, {"x": 0.5, "y": 0.5})
    for candidate in ("x", "y"):
        for index in (0, 1):
            requests.line(_evaluate_line(candidate, {"id": index}), io.StringIO())

    _report(requests, "x", 0.5, 1)
    _report(requests, "y", 0.5, 2)

    assert requests.best == {"candidate": "x", "score": 0.5}


def test_an_evaluation_with_no_case_in_a_task_with_cases_is_refused_to_the_child(tmp_path: Path) -> None:
    """Answer an evaluation that names no case with an error that stops the child, and score nothing.

    Args:
        tmp_path: Pytest's temporary folder.
    """
    requests = _two_case_requests(tmp_path, {"x": 1.0})
    replies = io.StringIO()

    assert requests.line(_evaluate_line("x", None), replies) is True

    assert json.loads(replies.getvalue()) == {"id": "0", "error": "The evaluation names no case."}
    assert (requests.server.used, requests.evals) == (0, 0)


def _proposer_options(entry: str, setup: list[str] | None = None) -> SimpleNamespace:
    """Build native options whose contract file names a ``[proposer] entry`` (SKY-815).

    Args:
        entry: The ``[proposer] entry`` command line.
        setup: The ``[proposer] setup`` argv, or None.

    Returns:
        The options; the run has no model gateway, so the command gets no network grant.
    """
    repo = {
        "chunks": [],
        "editable_paths": [],
        "readonly_paths": [],
        "hidden_hashes": {},
        "file_proposer": True,
        "proposer_setup": setup or [],
        "proposer_hosts": [],
    }
    return SimpleNamespace(
        shinka={},
        shinka_models=None,
        budget_route=None,
        max_token_cost=None,
        repo=repo,
        proposer=SimpleNamespace(run_command=entry),
        gateway=None,
        model="test-model",
    )


def _propose(requests: shinka_evolve._Requests, request_id: str, files: dict[str, str], task: str) -> dict:
    """Send one ``propose`` request as the child does, and read the reply.

    Args:
        requests: The engine's request handler.
        request_id: The request id.
        files: The parent's files.
        task: The mutation task.

    Returns:
        The reply.
    """
    replies = io.StringIO()
    body = json.dumps({"id": request_id, "kind": "propose", "files": files, "task": task})
    assert requests.line(f"APP_NATIVE_RPC n {body}", replies) is True
    return json.loads(replies.getvalue())


def test_each_proposal_runs_the_file_command_on_the_parents_tree_and_files_the_tree_it_leaves(
    owner: tuple[User, int], engine: Engine, step_log: StepLogStore, trees: TreeStore, tmp_path: Path
) -> None:
    """Run ``[proposer] entry`` once for each mutation, after ``[proposer] setup`` runs once (SKY-815).

    The command sees the parent's files and the task in ``ASCENT_TASK``. The child version is the text files of
    the tree the command leaves, without the setup's output and the task file.

    Args:
        owner: Alice and her budget generation.
        engine: The database.
        step_log: The step log.
        trees: The tree store.
        tmp_path: Pytest's temporary folder.
    """
    loop, _, boxes = _loop(owner, engine, step_log, trees, tmp_path, generation=1)
    ctx = loop.context(repo_seed=RepoSeed(_tree(trees, "main.py", "seed\n")))
    options = _proposer_options(
        'cp main.py parent.txt && cp "$ASCENT_TASK" seen.md && printf "%s" "$APP_MODEL" > model.txt'
        " && printf 'child\\n' > main.py",
        setup=["sh", "-c", "printf built > .cache.txt"],
    )
    proposer = shinka_evolve._Proposer(ctx, options)
    requests = shinka_evolve._Requests(
        Task("seed"), loop.server(lambda *args: (1.0, {}), max_evals=5), ctx, "n", proposer
    )

    first = _propose(requests, "1", {"main.py": "parent\n"}, "Improve this program:\nparent\n")
    second = _propose(requests, "2", {"main.py": "child\n"}, "Improve this program:\nchild\n")

    assert first == {
        "id": "1",
        "files": {
            "main.py": "child\n",
            "model.txt": "test-model",
            "parent.txt": "parent\n",
            "seen.md": "Improve this program:\nparent\n",
        },
    }
    assert second["files"]["parent.txt"] == "child\n"
    assert second["files"]["seen.md"] == "Improve this program:\nchild\n"
    # One setup command and one command for each proposal.
    assert [kind for _, kind, _ in _steps(engine)].count("command") == 3
    assert not any(boxes.open.values())


def test_a_version_leaves_out_unchanged_seed_files_setup_output_and_large_files(
    owner: tuple[User, int], engine: Engine, step_log: StepLogStore, trees: TreeStore, tmp_path: Path
) -> None:
    """Keep only the text files that differ from the seed, within the size limit, and no setup output (SKY-815).

    A file with a NUL byte or with bytes that are not UTF-8 is not text, so it stays out too. A setup output
    that the parent holds stays, since it came from an earlier version and not from the setup.

    Args:
        owner: Alice and her budget generation.
        engine: The database.
        step_log: The step log.
        trees: The tree store.
        tmp_path: Pytest's temporary folder.
    """
    loop, _, _ = _loop(owner, engine, step_log, trees, tmp_path, generation=1)
    seed = loop_tests._store(trees, {"main.py": b"seed\n", "other.py": b"same\n"})
    ctx = loop.context(repo_seed=RepoSeed(seed))
    limit = shinka_bundle.MAX_FILE_BYTES
    options = _proposer_options(
        "printf edited > .cache.txt && printf mine > gen.txt && printf 'child\\n' > main.py"
        f" && head -c {limit} /dev/zero | tr '\\0' a > edge.txt"
        f" && head -c {limit + 1} /dev/zero | tr '\\0' a > big.txt"
        " && printf 'a\\000b' > binary.dat && printf '\\377\\376' > latin.txt",
        setup=["sh", "-c", "printf built > .cache.txt && printf built > gen.txt"],
    )
    requests = shinka_evolve._Requests(
        Task("seed"),
        loop.server(lambda *args: (1.0, {}), max_evals=5),
        ctx,
        "n",
        shinka_evolve._Proposer(ctx, options),
    )

    reply = _propose(requests, "1", {"main.py": "parent\n", "gen.txt": "old"}, "task")

    assert reply == {"id": "1", "files": {"main.py": "child\n", "gen.txt": "mine", "edge.txt": "a" * limit}}


def test_a_failed_proposer_command_is_a_failed_mutation_and_a_failed_setup_stops_the_run(
    owner: tuple[User, int], engine: Engine, step_log: StepLogStore, trees: TreeStore, tmp_path: Path
) -> None:
    """Send a failed command's exit code and stderr back as the mutation's error, and stop on a failed setup.

    Args:
        owner: Alice and her budget generation.
        engine: The database.
        step_log: The step log.
        trees: The tree store.
        tmp_path: Pytest's temporary folder.
    """
    loop, _, _ = _loop(owner, engine, step_log, trees, tmp_path, generation=1)
    ctx = loop.context(repo_seed=RepoSeed(_tree(trees, "main.py", "seed\n")))
    server = loop.server(lambda *args: (1.0, {}), max_evals=5)
    failing = shinka_evolve._Proposer(ctx, _proposer_options("echo no model >&2; exit 3"))
    requests = shinka_evolve._Requests(Task("seed"), server, ctx, "n", failing)

    reply = _propose(requests, "1", {"main.py": "parent\n"}, "task")

    assert reply == {"id": "1", "error": "The [proposer] entry failed with exit code 3. no model"}
    broken = shinka_evolve._Proposer(ctx, _proposer_options("true", setup=["sh", "-c", "exit 4"]))
    with pytest.raises(ServiceError, match=r"\[proposer\] setup .* exit code 4"):
        broken.propose({}, "task")
    with pytest.raises(ServiceError, match="malformed proposal"):
        requests.line('APP_NATIVE_RPC n {"id": "2", "kind": "propose", "files": {"a": 1}, "task": "t"}', io.StringIO())


def test_a_setup_that_changes_or_removes_a_seed_file_stops_the_run(
    owner: tuple[User, int], engine: Engine, step_log: StepLogStore, trees: TreeStore, tmp_path: Path
) -> None:
    """Stop the run and name the files when ``[proposer] setup`` changes or removes a seed file (SKY-815).

    Every session starts from the setup's tree, so the change would enter every version.

    Args:
        owner: Alice and her budget generation.
        engine: The database.
        step_log: The step log.
        trees: The tree store.
        tmp_path: Pytest's temporary folder.
    """
    loop, _, _ = _loop(owner, engine, step_log, trees, tmp_path, generation=1)
    seed = loop_tests._store(trees, {"main.py": b"seed\n", "other.py": b"same\n", "kept.py": b"k\n"})
    ctx = loop.context(repo_seed=RepoSeed(seed))
    setup = ["sh", "-c", "printf x > main.py && rm other.py && printf new > new.txt"]
    proposer = shinka_evolve._Proposer(ctx, _proposer_options("true", setup=setup))

    with pytest.raises(ServiceError) as stopped:
        proposer.propose({}, "task")

    assert str(stopped.value) == (
        f"The [proposer] setup command in ascent.toml ({shlex.join(setup)}) changed or removed files of the"
        " repository: main.py, other.py. Those changes would enter every version. Make the setup write only new"
        " files."
    )


def test_a_version_reads_only_the_files_the_command_changed(
    owner: tuple[User, int], engine: Engine, step_log: StepLogStore, trees: TreeStore, tmp_path: Path
) -> None:
    """Compare content ids, and read no file that the command left as the parent or the setup had it (SKY-815).

    Args:
        owner: Alice and her budget generation.
        engine: The database.
        step_log: The step log.
        trees: The tree store.
        tmp_path: Pytest's temporary folder.
    """
    loop, _, _ = _loop(owner, engine, step_log, trees, tmp_path, generation=1)
    seed = loop_tests._store(trees, {"main.py": b"seed\n", "other.py": b"same\n"})
    ctx = loop.context(repo_seed=RepoSeed(seed))
    assert ctx.read_file is not None
    real_read = ctx.read_file
    reads: list[str] = []

    def read_file(tree_id: str, path: str) -> bytes | None:
        """Record each read and pass it on.

        Args:
            tree_id: The tree.
            path: The file.

        Returns:
            The file bytes.
        """
        reads.append(path)
        return real_read(tree_id, path)

    ctx = replace(ctx, read_file=read_file)
    options = _proposer_options("printf 'new\\n' > new.py", setup=["sh", "-c", "printf built > .cache.txt"])
    proposer = shinka_evolve._Proposer(ctx, options)

    files = proposer.propose({"main.py": "parent\n", "kept.py": "k\n"}, "task")

    assert files == {"files": {"main.py": "parent\n", "kept.py": "k\n", "new.py": "new\n"}}
    assert reads == ["new.py"]


def _failed_command(tmp_path: Path, result: SimpleNamespace) -> dict[str, Any]:
    """Run one proposal whose fake command gives the result.

    Args:
        tmp_path: Pytest's temporary folder.
        result: The command's result.

    Returns:
        The proposal's reply.
    """
    options = _proposer_options("node agent.js")
    ctx = _box_context(options, tmp_path, run_command=lambda *args: (result, "after"))
    return shinka_evolve._Proposer(ctx, options).propose({"main.py": "parent\n"}, "task")


def test_a_command_that_runs_out_of_time_is_a_failed_mutation_also_with_exit_code_0(tmp_path: Path) -> None:
    """Fail the mutation of a command that ran out of time, whatever its exit code (SKY-815).

    Args:
        tmp_path: Pytest's temporary folder.
    """
    result = SimpleNamespace(timed_out=True, exit_code=0, stderr="", stdout="")

    assert _failed_command(tmp_path, result) == {"error": "The [proposer] entry ran out of time."}


def test_a_failed_command_keeps_the_end_of_a_long_stderr(tmp_path: Path) -> None:
    """Keep the end of the command's stderr, where the error is, in the mutation's error (SKY-815).

    Args:
        tmp_path: Pytest's temporary folder.
    """
    stderr = "START" + "x" * (shinka_evolve._COMMAND_ERROR_CHARS * 2) + "END"
    result = SimpleNamespace(timed_out=False, exit_code=1, stderr=stderr, stdout="")

    error = _failed_command(tmp_path, result)["error"]

    assert error.endswith("xEND")
    assert "START" not in error


def test_a_run_with_a_file_proposer_is_refused_before_the_child_starts(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Refuse a context with no loop, refuse the command on the loop, and need the loop's boxes (SKY-815).

    Args:
        tmp_path: Pytest's temporary folder.
        monkeypatch: Fails the test when the child starts.
    """
    monkeypatch.setattr(ShinkaEvolveEngine, "_drive", lambda *args: pytest.fail("the child started"))
    options = _proposer_options("node agent.js")
    no_boxes = EngineContext(
        reflection_lm=lambda prompt: "",
        run_dir=str(tmp_path / "a"),
        call_model=lambda messages, tools: None,  # type: ignore[arg-type,return-value]
        native_options=options,  # type: ignore[arg-type]
        repo_seed=RepoSeed("tree"),
    )
    sandbox = replace(no_boxes, call_model=None)
    with pytest.raises(ServiceError, match="only on the worker engine loop"):
        ShinkaEvolveEngine(_fake_python(tmp_path)).run(
            Task("seed"), EvalServer(lambda c, e: (1.0, {}), max_evals=5), sandbox
        )
    with pytest.raises(ServiceError, match="model route on your own key"):
        ShinkaEvolveEngine(_fake_python(tmp_path)).run(
            Task("seed"), EvalServer(lambda c, e: (1.0, {}), max_evals=5), _box_context(options, tmp_path)
        )
    with pytest.raises(ServiceError, match="engine loop's boxes"):
        shinka_evolve._Proposer(no_boxes, options)


def test_a_proposal_leaves_git_metadata_out_of_the_version(
    owner: tuple[User, int], engine: Engine, step_log: StepLogStore, trees: TreeStore, tmp_path: Path
) -> None:
    """Leave a ``.git`` folder that the command writes out of the version, because the box does not keep it (SKY-815).

    Args:
        owner: Alice and her budget generation.
        engine: The database.
        step_log: The step log.
        trees: The tree store.
        tmp_path: Pytest's temporary folder.
    """
    loop, _, _ = _loop(owner, engine, step_log, trees, tmp_path, generation=1)
    ctx = loop.context(repo_seed=RepoSeed(_tree(trees, "main.py", "seed\n")))
    options = _proposer_options(
        "mkdir -p sub/.GIT/hooks && printf x > sub/.GIT/hooks/post-commit && printf 'child\\n' > main.py"
    )
    requests = shinka_evolve._Requests(
        Task("seed"),
        loop.server(lambda *args: (1.0, {}), max_evals=5),
        ctx,
        "n",
        shinka_evolve._Proposer(ctx, options),
    )

    assert _propose(requests, "1", {"main.py": "parent\n"}, "task") == {"id": "1", "files": {"main.py": "child\n"}}


def test_the_version_leaves_out_each_git_part_that_the_tree_holds(tmp_path: Path) -> None:
    """Leave ``.git`` parts, in any letter case, out of a version even when the tree holds them (SKY-815).

    Args:
        tmp_path: Pytest's temporary folder.
    """
    trees = {"after": {"main.py": "a", "sub/.GIT/x": "b", ".git/config": "c"}, "parent": {}}
    ctx = _box_context(
        _proposer_options("node agent.js"),
        tmp_path,
        file_ids=lambda tree: trees[tree],
        read_file=lambda tree, path: b"text\n",
    )

    version = shinka_evolve._Proposer(ctx, _proposer_options("node agent.js"))._version("after", "parent", {})

    assert version == {"main.py": "text\n"}


def test_a_command_that_leaves_too_many_files_is_a_failed_mutation_with_no_read(tmp_path: Path) -> None:
    """Fail the mutation of a command that adds more files than a tree listing holds, before any read (SKY-815).

    A command that runs ``npm install`` leaves thousands of ignored files; reading each would cost thousands of
    reads and a reply of many megabytes.

    Args:
        tmp_path: Pytest's temporary folder.
    """
    count = shinka_bundle._MAX_TREE_ENTRIES + 1
    trees = {"after": {f"node_modules/m{index}.js": str(index) for index in range(count)}, "parent": {}}
    reads: list[str] = []
    options = _proposer_options("npm install")
    result = SimpleNamespace(timed_out=False, exit_code=0, stderr="", stdout="")
    ctx = _box_context(
        options,
        tmp_path,
        run_command=lambda *args: (result, "after"),
        file_ids=lambda tree: trees.get(tree, {}),
        read_file=lambda tree, path: reads.append(path) or b"text\n",
    )

    reply = shinka_evolve._Proposer(ctx, options).propose({}, "task")

    assert reply == {
        "error": f"The [proposer] entry command added or changed more than {shinka_bundle._MAX_TREE_ENTRIES} files."
        " Make it write its tools and caches outside the repository, or remove them before it exits."
    }
    assert reads == []


def test_a_command_that_leaves_exactly_the_file_cap_is_read_in_full(tmp_path: Path) -> None:
    """Read every file of a command that changes exactly as many files as a tree listing holds (SKY-815).

    Args:
        tmp_path: Pytest's temporary folder.
    """
    paths = [f"gen/m{index}.js" for index in range(shinka_bundle._MAX_TREE_ENTRIES)]
    trees = {"after": {path: path for path in paths}, "parent": {}}
    reads: list[str] = []
    options = _proposer_options("node gen.js")
    result = SimpleNamespace(timed_out=False, exit_code=0, stderr="", stdout="")
    ctx = _box_context(
        options,
        tmp_path,
        run_command=lambda *args: (result, "after"),
        file_ids=lambda tree: trees.get(tree, {}),
        read_file=lambda tree, path: reads.append(path) or b"text\n",
    )

    reply = shinka_evolve._Proposer(ctx, options).propose({}, "task")

    assert reply == {"files": dict.fromkeys(paths, "text\n")}
    assert sorted(reads) == sorted(paths)


_BYOK_ROUTE = {"url": "http://127.0.0.1:4100/v1", "token": "byok-route", "model": "m", "role": "optimization"}


@pytest.mark.parametrize(
    ("route", "token_cost", "message"),
    [
        ({MCP_RELAY_ROUTE_KEY: {"servers": [{"name": "docs"}]}}, None, r"\[proposer\] mcp servers"),
        ({MCP_RELAY_ROUTE_KEY: {"services": {"api": {}}}}, None, r"\[proposer\] services"),
        # A BYOK route has no token budget; a managed route has one. Neither is a route a box can reach.
        (_BYOK_ROUTE, None, "model route on your own key"),
        (_BYOK_ROUTE, 1.5, "model route on your own key"),
        # With no route, the run's gateway is the deployment's, with its managed key.
        (None, None, "model route on your own key"),
        (None, 1.5, "model route on your own key"),
    ],
)
def test_a_file_proposer_is_refused_on_every_route(
    route: dict[str, object] | None,
    token_cost: float | None,
    message: str,
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Refuse the ``[proposer] entry`` command on BYOK, managed and routeless runs alike (SKY-815).

    No run can give the box a model route on the user's own key until a metered relay (SKY-828), and the box must
    never reach the deployment's key.

    Args:
        route: The run's budget route.
        token_cost: The run's token budget, which only a managed run has.
        message: Words the refusal holds.
        tmp_path: Pytest's temporary folder.
        monkeypatch: Fails the test when the child starts.
    """
    monkeypatch.setattr(ShinkaEvolveEngine, "_drive", lambda *args: pytest.fail("the child started"))
    options = _proposer_options("node agent.js")
    options.budget_route = route
    options.max_token_cost = token_cost
    options.gateway = SimpleNamespace(url="https://gw.test/v1")
    ctx = EngineContext(
        reflection_lm=lambda prompt: "",
        run_dir=str(tmp_path),
        call_model=lambda messages, tools: None,  # type: ignore[arg-type,return-value]
        native_options=options,  # type: ignore[arg-type]
        run_command=lambda *args: None,  # type: ignore[arg-type,return-value]
        put_files=lambda *args: None,  # type: ignore[arg-type,return-value]
        read_file=lambda *args: None,
        list_files=lambda *args: None,
        repo_seed=RepoSeed("tree"),
    )

    with pytest.raises(ServiceError, match=message):
        ShinkaEvolveEngine(_fake_python(tmp_path)).run(
            Task("seed"), EvalServer(lambda c, e: (1.0, {}), max_evals=5), ctx
        )


def _box_context(options: SimpleNamespace, tmp_path: Path, **fields: object) -> EngineContext:
    """Build a loop context whose boxes are fakes, for the checks before and around a command.

    Args:
        options: The run's native options.
        tmp_path: Pytest's temporary folder.
        fields: Context fields that replace the fakes.

    Returns:
        The context.
    """
    ctx = EngineContext(
        reflection_lm=lambda prompt: "",
        run_dir=str(tmp_path),
        call_model=lambda messages, tools: None,  # type: ignore[arg-type,return-value]
        native_options=options,  # type: ignore[arg-type]
        run_command=lambda *args: None,  # type: ignore[arg-type,return-value]
        put_files=lambda *args: "parent",  # type: ignore[arg-type,return-value]
        read_file=lambda *args: None,
        list_files=lambda *args: [],
        file_ids=lambda *args: {},
        repo_seed=RepoSeed("tree"),
    )
    return replace(ctx, **fields)  # type: ignore[arg-type]


@pytest.mark.parametrize("route", [_BYOK_ROUTE, None])
def test_the_proposer_box_gets_each_host_once_and_never_the_gateway(
    route: dict[str, object] | None, tmp_path: Path
) -> None:
    """Grant each ``proposer_hosts`` host once, with no credential, and never name the run's gateway (SKY-815).

    Args:
        route: The run's budget route.
        tmp_path: Pytest's temporary folder.
    """
    options = _proposer_options("node agent.js")
    options.budget_route = route
    options.gateway = SimpleNamespace(url="https://gw.test/v1")
    options.repo["proposer_hosts"] = ["pypi.org", "gw.test", "docs.test", "pypi.org"]

    proposer = shinka_evolve._Proposer(_box_context(options, tmp_path), options)

    assert proposer.grants == (Grant("docs.test"), Grant("pypi.org"))
    assert proposer.exports == ["export APP_MODEL=test-model"]


def test_a_proposal_whose_grants_do_not_resolve_stops_the_run(
    owner: tuple[User, int], engine: Engine, step_log: StepLogStore, trees: TreeStore, tmp_path: Path
) -> None:
    """Stop the run when the loop cannot resolve the proposer box's grants (SKY-815).

    Each mutation would fail the same way, so the error must not become a failed mutation.

    Args:
        owner: Alice and her budget generation.
        engine: The database.
        step_log: The step log.
        trees: The tree store.
        tmp_path: Pytest's temporary folder.
    """
    options = _proposer_options("printf 'child\\n' > main.py")
    options.repo["proposer_hosts"] = ["bad host"]
    loop, _, _ = _loop(owner, engine, step_log, trees, tmp_path, generation=1)
    ctx = loop.context(repo_seed=RepoSeed(_tree(trees, "main.py", "seed\n")))

    with pytest.raises(GrantDeniedError, match="bad host"):
        shinka_evolve._Proposer(ctx, options).propose({"main.py": "parent\n"}, "task")

    def refuse(*args: object) -> None:
        """Refuse the grants as a run command that resolves them itself does.

        Args:
            args: The command's tree, script and grants.

        Raises:
            UnknownCapabilityError: Always.
        """
        raise UnknownCapabilityError("gw.test")

    fake = _box_context(options, tmp_path, run_command=refuse)
    with pytest.raises(UnknownCapabilityError):
        shinka_evolve._Proposer(fake, options).propose({"main.py": "parent\n"}, "task")


def test_a_proposal_checks_the_budget_before_the_command_runs(tmp_path: Path) -> None:
    """Stop at the budget check, before the proposer box starts (SKY-815).

    Args:
        tmp_path: Pytest's temporary folder.
    """
    commands: list[object] = []

    def run_command(*args: object) -> None:
        """Record a command that must not run.

        Args:
            args: The command's tree, script and grants.
        """
        commands.append(args)

    def check_budget() -> None:
        """Refuse the run's next paid step.

        Raises:
            BudgetReached: Always.
        """
        raise BudgetReached()

    options = _proposer_options("node agent.js")
    ctx = _box_context(options, tmp_path, run_command=run_command, check_budget=check_budget)
    requests = shinka_evolve._Requests(
        Task("seed"), EvalServer(lambda c, e: (1.0, {}), max_evals=5), ctx, "n", shinka_evolve._Proposer(ctx, options)
    )

    with pytest.raises(BudgetReached):
        _propose(requests, "1", {"main.py": "parent\n"}, "task")
    assert commands == []


class _ScriptedAgent:
    """A fake agent model: the reply for each turn comes from a script, by the turn's number (SKY-830).

    The turn is the number of assistant messages in the prompt, so a resumed run asks for the same replies.
    """

    def __init__(self, script: list[ModelReply | Exception], die_at: int | None = None) -> None:
        """Set the replies.

        Args:
            script: The reply, or the error to raise, for each turn.
            die_at: The turn in which the worker dies; ``None`` never dies.
        """
        self.script = script
        self.die_at = die_at
        self.prompts: list[list[dict[str, Any]]] = []
        self.tools: list[object] = []

    def __call__(self, prompt: list[dict[str, Any]], tools: object) -> ModelReply:
        """Answer one turn from the script.

        Args:
            prompt: The session's messages.
            tools: The tool definitions.

        Returns:
            The turn's reply.

        Raises:
            _WorkerDiedError: In the turn at ``die_at``.
            Exception: The turn's scripted error.
        """
        turn = sum(1 for message in prompt if message["role"] == "assistant")
        if turn == self.die_at:
            raise _WorkerDiedError
        # The session appends to the same list, so keep the messages as this call saw them.
        self.prompts.append(list(prompt))
        self.tools.append(tools)
        reply = self.script[turn]
        if isinstance(reply, Exception):
            raise reply
        return reply


def _call(index: int, name: str, **arguments: str) -> dict[str, Any]:
    """Build one tool call in the loop's flat form.

    Args:
        index: The call's number.
        name: The tool.
        **arguments: The tool's arguments.

    Returns:
        The call.
    """
    return {"id": f"call_{index}", "name": name, "arguments": arguments}


_EDIT_SCRIPT: list[ModelReply | Exception] = [
    ModelReply("", Decimal(1), [_call(0, "read_file", path="main.py")]),  # type: ignore[list-item]
    ModelReply(
        "",
        Decimal(1),
        [  # type: ignore[list-item]
            _call(1, "write_file", path="main.py", content="child\n"),
            _call(2, "run", command="cp main.py out.txt"),
        ],
    ),
    ModelReply("Name: copy\nDescription: Copy the program.", Decimal(1)),
]


def _agent_options(**repo: object) -> SimpleNamespace:
    """Build native options for the agent editor.

    Args:
        **repo: Repository option fields that replace the defaults.

    Returns:
        The options.
    """
    return SimpleNamespace(
        shinka={"editor": "agent"},
        budget_route=None,
        repo={"proposer_hosts": [], "proposer_mcp": [], **repo},
        proposer=SimpleNamespace(max_tool_calls=None),
        gateway=None,
    )


def _agent_request(requests: shinka_evolve._Requests, files: dict[str, str], task: str) -> dict:
    """Send one ``agent`` request as the child's agent editor does, and read the reply.

    Args:
        requests: The engine's request handler.
        files: The parent's files.
        task: The agent's task.

    Returns:
        The reply.
    """
    replies = io.StringIO()
    body = json.dumps({"id": "1", "kind": "agent", "files": files, "task": task})
    assert requests.line(f"APP_NATIVE_RPC n {body}", replies) is True
    return json.loads(replies.getvalue())


def _agent_requests(loop: Any, trees: TreeStore) -> shinka_evolve._Requests:
    """Build a request handler whose agent editor runs on the loop over a one-file seed.

    Args:
        loop: The engine loop.
        trees: The tree store.

    Returns:
        The handler.
    """
    ctx = loop.context(repo_seed=RepoSeed(_tree(trees, "main.py", "seed\n")))
    session = shinka_evolve._AgentSession(ctx, _agent_options(), [])
    server = loop.server(lambda *args: (1.0, {}), max_evals=5)
    return shinka_evolve._Requests(Task("seed"), server, ctx, "n", agent=session)


def test_each_agent_reply_is_a_model_step_and_each_run_call_a_command_step(
    owner: tuple[User, int], engine: Engine, step_log: StepLogStore, trees: TreeStore, tmp_path: Path
) -> None:
    """Run the agent editor's tool session on the loop, and send the files it leaves back as the version (SKY-830).

    Args:
        owner: Alice and her budget generation.
        engine: The database.
        step_log: The step log.
        trees: The tree store.
        tmp_path: Pytest's temporary folder.
    """
    loop, _, boxes = _loop(owner, engine, step_log, trees, tmp_path, generation=1)
    model = _ScriptedAgent(_EDIT_SCRIPT)
    loop._model = model
    requests = _agent_requests(loop, trees)

    reply = _agent_request(requests, {"main.py": "parent\n"}, "Improve main.py.")

    assert reply == {
        "id": "1",
        "files": {"main.py": "child\n", "out.txt": "child\n"},
        "text": "Name: copy\nDescription: Copy the program.",
        "tool_calls": 3,
        "tool_cap_reached": False,
    }
    assert [kind for _, kind, _ in _steps(engine)] == ["model", "model", "command", "model"]
    assert requests.cost_cents == 3
    # The model reads the parent's file, not the seed's, and sees the run's exit code.
    assert model.prompts[1][-1] == {"role": "tool", "tool_call_id": "call_0", "content": "parent\n"}
    assert model.prompts[2][-1]["content"].startswith("exit code 0")
    assert model.tools == [shinka_evolve._AGENT_TOOLS] * 3
    assert boxes.opened == 1
    assert not any(boxes.open.values())


def test_an_agent_session_killed_in_a_model_call_resumes_from_its_last_committed_step(
    owner: tuple[User, int], engine: Engine, step_log: StepLogStore, trees: TreeStore, tmp_path: Path
) -> None:
    """Kill the worker in the session's last model call, then resume: the log replays, only the last call runs.

    Args:
        owner: Alice and her budget generation.
        engine: The database.
        step_log: The step log.
        trees: The tree store.
        tmp_path: Pytest's temporary folder.
    """
    first, _, _ = _loop(owner, engine, step_log, trees, tmp_path, generation=1)
    first._model = _ScriptedAgent(_EDIT_SCRIPT, die_at=2)
    with pytest.raises(_WorkerDiedError):
        _agent_request(_agent_requests(first, trees), {"main.py": "parent\n"}, "Improve main.py.")
    assert [kind for _, kind, _ in _steps(engine)] == ["model", "model", "command"]

    _new_generation(engine)
    loop, _, boxes = _loop(owner, engine, step_log, trees, tmp_path, generation=2)
    model = _ScriptedAgent(_EDIT_SCRIPT)
    loop._model = model
    requests = _agent_requests(loop, trees)
    reply = _agent_request(requests, {"main.py": "parent\n"}, "Improve main.py.")

    assert reply["files"] == {"main.py": "child\n", "out.txt": "child\n"}
    assert len(model.prompts) == 1
    assert boxes.opened == 0
    assert [generation for _, _, generation in _steps(engine)] == [1, 1, 1, 2]
    # The replayed steps keep their cost.
    assert requests.cost_cents == 3


def test_a_billed_failed_agent_call_charges_its_cost_and_fails_only_the_mutation(
    owner: tuple[User, int], engine: Engine, step_log: StepLogStore, trees: TreeStore, tmp_path: Path
) -> None:
    """Add a failed call's billed cost to the run's model cost, and send the failure back as the mutation's error.

    Args:
        owner: Alice and her budget generation.
        engine: The database.
        step_log: The step log.
        trees: The tree store.
        tmp_path: Pytest's temporary folder.
    """
    loop, _, _ = _loop(owner, engine, step_log, trees, tmp_path, generation=1)
    loop._model = _ScriptedAgent([_EDIT_SCRIPT[0], ModelCallBilledError("1.5")])
    requests = _agent_requests(loop, trees)

    reply = _agent_request(requests, {"main.py": "parent\n"}, "task")

    assert reply["error"].startswith("The agent editor's model call failed")
    assert requests.cost_cents == Decimal("2.5")
    assert [kind for _, kind, _ in _steps(engine)] == ["model", "model"]


def test_a_budget_stop_in_an_agent_session_stops_the_run_before_the_next_command(tmp_path: Path) -> None:
    """Let a budget stop before a ``run`` call leave the session, with no command run (SKY-830).

    Args:
        tmp_path: Pytest's temporary folder.
    """
    checks: list[int] = []
    commands: list[object] = []
    models: list[object] = []

    def check_budget() -> None:
        """Stop at the third check: the one before the first command.

        Raises:
            BudgetReached: At the third check.
        """
        checks.append(1)
        if len(checks) == 3:
            raise BudgetReached

    def call_model(messages: object, tools: object) -> ModelReply:
        """Ask for one command.

        Args:
            messages: The session's messages.
            tools: The tool definitions.

        Returns:
            A reply with one ``run`` call.
        """
        models.append(messages)
        return ModelReply("", Decimal(1), [_call(0, "run", command="true")])  # type: ignore[list-item]

    options = _agent_options()
    ctx = _box_context(
        options, tmp_path, check_budget=check_budget, call_model=call_model, run_command=lambda *a: commands.append(a)
    )
    requests = shinka_evolve._Requests(
        Task("seed"),
        EvalServer(lambda *a: (1.0, {}), max_evals=5),
        ctx,
        "n",
        agent=shinka_evolve._AgentSession(ctx, options, []),
    )

    with pytest.raises(BudgetReached):
        _agent_request(requests, {"main.py": "parent\n"}, "task")
    assert (len(models), commands) == (1, [])


def test_an_agent_session_ends_at_the_tool_call_limit(tmp_path: Path) -> None:
    """Answer each call past the limit with the limit's note, end the session and mark the cap (SKY-830).

    Args:
        tmp_path: Pytest's temporary folder.
    """
    models: list[list[dict[str, Any]]] = []

    def call_model(messages: list[dict[str, Any]], tools: object) -> ModelReply:
        """Ask for two listings.

        Args:
            messages: The session's messages.
            tools: The tool definitions.

        Returns:
            A reply with two calls.
        """
        models.append(messages)
        calls = [_call(0, "list_files", path=""), _call(1, "list_files", path="")]
        return ModelReply("", Decimal(1), calls)  # type: ignore[arg-type]

    options = _agent_options()
    options.proposer.max_tool_calls = 1
    ctx = _box_context(options, tmp_path, call_model=call_model, list_files=lambda *a: ["main.py"])

    result = shinka_evolve._AgentSession(ctx, options, []).edit({}, "task", lambda cents: None)

    assert result == {"files": {}, "text": None, "tool_calls": 1, "tool_cap_reached": True}
    assert len(models) == 1


def test_an_agent_session_at_the_tool_call_limit_keeps_the_files_it_wrote(tmp_path: Path) -> None:
    """Return the version the session wrote before the limit, not the parent's files (SKY-830).

    Args:
        tmp_path: Pytest's temporary folder.
    """

    def call_model(messages: list[dict[str, Any]], tools: object) -> ModelReply:
        """Write a file, then ask for a listing past the limit.

        Args:
            messages: The session's messages.
            tools: The tool definitions.

        Returns:
            A reply with two calls.
        """
        calls = [_call(0, "write_file", path="new.py", content="new\n"), _call(1, "list_files", path="")]
        return ModelReply("", Decimal(1), calls)  # type: ignore[arg-type]

    def put_files(files: dict[str, bytes], base: str | None) -> str:
        """Name the parent's tree, or the tree after the write.

        Args:
            files: The files to write.
            base: The tree the files go on.

        Returns:
            The new tree.
        """
        return "parent" if base == "tree" else "child"

    ids = {"child": {"new.py": "new-oid"}}
    options = _agent_options()
    options.proposer.max_tool_calls = 1
    ctx = _box_context(
        options,
        tmp_path,
        call_model=call_model,
        put_files=put_files,
        file_ids=lambda tree: ids.get(tree, {}),
        read_file=lambda tree, path: b"new\n" if (tree, path) == ("child", "new.py") else None,
    )

    result = shinka_evolve._AgentSession(ctx, options, []).edit({}, "task", lambda cents: None)

    assert result == {"files": {"new.py": "new\n"}, "text": None, "tool_calls": 1, "tool_cap_reached": True}


def test_an_agent_run_that_leaves_too_many_files_is_an_error_with_no_read(tmp_path: Path) -> None:
    """Fail the session whose ``run`` adds more files than a tree listing holds, before any read (SKY-830).

    An agent that runs ``npm install`` leaves thousands of ignored files in the session's tree.

    Args:
        tmp_path: Pytest's temporary folder.
    """
    replies = [
        ModelReply("", Decimal(1), [_call(0, "run", command="npm install")]),  # type: ignore[list-item]
        ModelReply("done", Decimal(1), []),
    ]
    count = shinka_bundle._MAX_TREE_ENTRIES + 1
    trees = {"after": {f"node_modules/m{index}.js": str(index) for index in range(count)}}
    reads: list[str] = []
    result = SimpleNamespace(timed_out=False, exit_code=0, stderr="", stdout="")
    options = _agent_options()
    ctx = _box_context(
        options,
        tmp_path,
        call_model=lambda *a: replies.pop(0),
        put_files=lambda files, base: "parent",
        run_command=lambda *a: (result, "after"),
        file_ids=lambda tree: trees.get(tree, {}),
        read_file=lambda tree, path: reads.append(path) or b"text\n",
    )

    reply = shinka_evolve._AgentSession(ctx, options, []).edit({}, "task", lambda cents: None)

    assert reply == {
        "error": f"The agent editor's session added or changed more than {shinka_bundle._MAX_TREE_ENTRIES} files."
        " Make the agent write its tools and caches outside the repository, or remove them before it ends.",
        "tool_calls": 1,
        "tool_cap_reached": False,
    }
    assert reads == []


def test_an_agent_run_past_the_file_cap_at_the_tool_call_limit_reports_the_cap(tmp_path: Path) -> None:
    """Mark the tool-call cap in the error reply of a session that ends at the limit with too many files (SKY-830).

    The child reads the tool-call counts from every reply, an error reply too.

    Args:
        tmp_path: Pytest's temporary folder.
    """
    count = shinka_bundle._MAX_TREE_ENTRIES + 1
    trees = {"after": {f"node_modules/m{index}.js": str(index) for index in range(count)}}
    result = SimpleNamespace(timed_out=False, exit_code=0, stderr="", stdout="")
    options = _agent_options()
    options.proposer.max_tool_calls = 1
    ctx = _box_context(
        options,
        tmp_path,
        call_model=lambda *a: ModelReply("", Decimal(1), [_call(0, "run", command="npm install")]),  # type: ignore[list-item]
        put_files=lambda files, base: "parent",
        run_command=lambda *a: (result, "after"),
        file_ids=lambda tree: trees.get(tree, {}),
        read_file=lambda tree, path: b"text\n",
    )

    reply = shinka_evolve._AgentSession(ctx, options, []).edit({}, "task", lambda cents: None)

    assert "error" in reply
    assert reply["tool_calls"] == 1
    assert reply["tool_cap_reached"] is True


def test_an_agent_run_that_leaves_exactly_the_file_cap_is_read_in_full(tmp_path: Path) -> None:
    """Read every file of a session whose ``run`` changes exactly as many files as a tree listing holds (SKY-830).

    Args:
        tmp_path: Pytest's temporary folder.
    """
    replies = [
        ModelReply("", Decimal(1), [_call(0, "run", command="node gen.js")]),  # type: ignore[list-item]
        ModelReply("done", Decimal(1), []),
    ]
    paths = [f"gen/m{index}.js" for index in range(shinka_bundle._MAX_TREE_ENTRIES)]
    trees = {"after": {path: path for path in paths}}
    reads: list[str] = []
    result = SimpleNamespace(timed_out=False, exit_code=0, stderr="", stdout="")
    options = _agent_options()
    ctx = _box_context(
        options,
        tmp_path,
        call_model=lambda *a: replies.pop(0),
        put_files=lambda files, base: "parent",
        run_command=lambda *a: (result, "after"),
        file_ids=lambda tree: trees.get(tree, {}),
        read_file=lambda tree, path: reads.append(path) or b"text\n",
    )

    reply = shinka_evolve._AgentSession(ctx, options, []).edit({}, "task", lambda cents: None)

    assert reply == {
        "files": dict.fromkeys(paths, "text\n"),
        "text": "done",
        "tool_calls": 1,
        "tool_cap_reached": False,
    }
    assert sorted(reads) == sorted(paths)


@pytest.mark.parametrize(
    ("first", "cut"), [(shinka_evolve._TOOL_OUTPUT_CHARS - 2, False), (shinka_evolve._TOOL_OUTPUT_CHARS - 1, True)]
)
def test_a_file_list_is_cut_only_when_its_joined_text_passes_the_output_limit(first: int, cut: bool) -> None:
    """Keep a list that joins to exactly the output limit, and cut one that joins to one character more (SKY-830).

    The newlines go only between paths, so the joined text has one character fewer than the paths with a
    newline each.

    Args:
        first: The length of the first path; the second path is one character, with a newline between them.
        cut: Whether the list is cut.
    """
    listed = ["a" * first, "b"]

    text = shinka_evolve._listing_output(listed)

    if cut:
        assert text == f"{listed[0]}\n[cut: 1 of 2 files; use the run tool, for example find]"
    else:
        assert text == "\n".join(listed)
        assert len(text) == shinka_evolve._TOOL_OUTPUT_CHARS


def test_the_agent_box_grants_mcp_hosts_with_their_credential_and_never_the_gateway(tmp_path: Path) -> None:
    """Grant an MCP host with headers with ``credential=True``, so the edge adds them and no secret enters the box.

    Args:
        tmp_path: Pytest's temporary folder.
    """
    options = _agent_options(
        proposer_hosts=["pypi.org", "gw.test"],
        proposer_mcp=[
            {"name": "docs", "url": "https://mcp.test/mcp", "has_headers": True},
            {"name": "unused", "url": "https://x.test", "has_headers": True},
        ],
    )
    options.gateway = SimpleNamespace(url="https://gw.test/v1")

    session = shinka_evolve._AgentSession(_box_context(options, tmp_path), options, ["docs"])

    assert session.grants == (Grant("pypi.org"), Grant("mcp.test", credential=True, path="/mcp"))
    assert "- docs: https://mcp.test/mcp" in session._system()
    assert "unused" not in session._system()
    with pytest.raises(ServiceError, match="no address"):
        shinka_evolve._AgentSession(_box_context(options, tmp_path), options, ["missing"])


def test_the_agent_run_tool_sends_the_session_grants_to_the_box(tmp_path: Path) -> None:
    """Send the session's grants with each ``run`` call, so the box reaches its hosts and never the gateway (SKY-830).

    Args:
        tmp_path: Pytest's temporary folder.
    """
    sent: list[object] = []

    def run_command(tree: str, script: str, grants: object) -> tuple[SimpleNamespace, str]:
        """Record the grants and succeed.

        Args:
            tree: The session's tree.
            script: The command.
            grants: The box's grants.

        Returns:
            A clean result and the tree after the command.
        """
        sent.append(grants)
        return SimpleNamespace(timed_out=False, exit_code=0, stdout="ok", stderr=""), "after"

    options = _agent_options(
        proposer_hosts=["pypi.org", "gw.test"],
        proposer_mcp=[{"name": "docs", "url": "https://mcp.test/mcp", "has_headers": True}],
    )
    options.gateway = SimpleNamespace(url="https://gw.test/v1")
    session = shinka_evolve._AgentSession(_box_context(options, tmp_path, run_command=run_command), options, ["docs"])

    _, tree = session._tool(_call(0, "run", command="true"), "tree")

    assert tree == "after"
    assert sent == [(Grant("pypi.org"), Grant("mcp.test", credential=True, path="/mcp"))]


def test_the_agent_box_grants_an_mcp_host_with_no_headers_without_a_credential(tmp_path: Path) -> None:
    """Grant a public MCP server's host plainly, since a credential grant with no headers fails to resolve.

    Args:
        tmp_path: Pytest's temporary folder.
    """
    options = _agent_options(
        proposer_hosts=["pypi.org"],
        proposer_mcp=[
            {"name": "public", "url": "https://open.test/mcp", "has_headers": False},
            {"name": "docs", "url": "https://mcp.test/mcp", "has_headers": True},
        ],
    )

    session = shinka_evolve._AgentSession(_box_context(options, tmp_path), options, ["public", "docs"])

    assert session.grants == (Grant("open.test"), Grant("pypi.org"), Grant("mcp.test", credential=True, path="/mcp"))


def test_two_mcp_servers_with_headers_on_one_host_are_refused(tmp_path: Path) -> None:
    """Refuse two servers with headers on one host, and name them, since one host takes one prefix (SKY-833).

    Args:
        tmp_path: Pytest's temporary folder.
    """
    options = _agent_options(
        proposer_hosts=[],
        proposer_mcp=[
            {"name": "team", "url": "https://mcp.test/team/mcp", "has_headers": True},
            {"name": "web", "url": "https://mcp.test/web", "has_headers": True},
            {"name": "root", "url": "https://root.test", "has_headers": True},
        ],
    )

    with pytest.raises(ServiceError, match=r"\['team', 'web'\] share the host 'mcp.test', and \['team', 'web'\]"):
        shinka_evolve._AgentSession(_box_context(options, tmp_path), options, ["team", "web", "root"])


@pytest.mark.parametrize("url", ["https://mcp.test/web", "https://mcp.test/team/mcp", "https://mcp.test/team/mcp/"])
def test_an_mcp_server_with_headers_shares_its_host_with_no_other_server(tmp_path: Path, url: str) -> None:
    """Refuse any other server on a host with headers: '/team/../web' would take the headers there (SKY-833).

    Args:
        tmp_path: Pytest's temporary folder.
        url: The address of a server with no headers on the same host.
    """
    options = _agent_options(
        proposer_hosts=[],
        proposer_mcp=[
            {"name": "team", "url": "https://mcp.test/team/mcp", "has_headers": True},
            {"name": "web", "url": url, "has_headers": False},
        ],
    )

    with pytest.raises(ServiceError, match=r"\['team', 'web'\] share the host 'mcp.test', and \['team'\] send"):
        shinka_evolve._AgentSession(_box_context(options, tmp_path), options, ["team", "web"])


@pytest.mark.parametrize(
    ("team", "web"),
    [
        ("https://mcp.test/team/mcp", "https://mcp.test./web"),
        ("https://mcp.test/team/mcp", "https://MCP.test/web"),
        ("https://mcp.test./team/mcp", "https://mcp.test/web"),
    ],
)
def test_a_trailing_dot_or_capital_does_not_split_a_host_with_headers(tmp_path: Path, team: str, web: str) -> None:
    """Refuse a server on a host with headers when only a trailing dot or a capital tells the hosts apart (SKY-833).

    The grants normalize both forms to one host, so the edge would add the headers to both servers.

    Args:
        tmp_path: Pytest's temporary folder.
        team: The address of the server with headers.
        web: The address of a server with no headers on the same host.
    """
    options = _agent_options(
        proposer_hosts=[],
        proposer_mcp=[
            {"name": "team", "url": team, "has_headers": True},
            {"name": "web", "url": web, "has_headers": False},
        ],
    )

    with pytest.raises(ServiceError, match=r"\['team', 'web'\] share the host 'mcp.test', and \['team'\] send"):
        shinka_evolve._AgentSession(_box_context(options, tmp_path), options, ["team", "web"])


@pytest.mark.parametrize(
    ("url", "network"),
    [
        ("https://mcp.test/team/mcp", "MCP.test"),
        ("https://mcp.test/team/mcp", "mcp.test."),
        ("https://mcp.test./team/mcp", "mcp.test"),
        ("https://mcp.test/team/mcp", "*.test"),
        ("https://api.mcp.test/team/mcp", "*.mcp.test"),
    ],
)
def test_a_proposer_network_host_that_covers_an_mcp_host_with_headers_is_refused(
    tmp_path: Path, url: str, network: str
) -> None:
    """Refuse a network host or wildcard that covers a host with headers, since the box would reach its paths.

    Args:
        tmp_path: Pytest's temporary folder.
        url: The address of the server with headers.
        network: A host the proposer's network reaches.
    """
    options = _agent_options(
        proposer_hosts=["pypi.org", network],
        proposer_mcp=[{"name": "team", "url": url, "has_headers": True}],
    )

    with pytest.raises(ServiceError, match=r"server 'team' sends headers, and the proposer's network also reaches"):
        shinka_evolve._AgentSession(_box_context(options, tmp_path), options, ["team"])


def test_a_proposer_network_wildcard_of_the_host_itself_is_refused(tmp_path: Path) -> None:
    """Refuse ``*.mcp.test`` next to a server with headers on ``mcp.test``: the edge may match the bare suffix.

    Args:
        tmp_path: Pytest's temporary folder.
    """
    options = _agent_options(
        proposer_hosts=["*.mcp.test."],
        proposer_mcp=[{"name": "team", "url": "https://mcp.test/team/mcp", "has_headers": True}],
    )

    with pytest.raises(ServiceError, match=r"server 'team' sends headers, and the proposer's network also reaches"):
        shinka_evolve._AgentSession(_box_context(options, tmp_path), options, ["team"])


def test_mcp_servers_on_other_hosts_each_get_their_own_grant(tmp_path: Path) -> None:
    """Grant each server on its own host a credential for its path, so the edge adds its headers only there.

    Args:
        tmp_path: Pytest's temporary folder.
    """
    options = _agent_options(
        proposer_hosts=[],
        proposer_mcp=[
            {"name": "team", "url": "https://mcp.test/team/mcp", "has_headers": True},
            {"name": "root", "url": "https://root.test", "has_headers": True},
            {"name": "public", "url": "https://open.test/mcp", "has_headers": False},
        ],
    )

    session = shinka_evolve._AgentSession(_box_context(options, tmp_path), options, ["team", "root", "public"])

    assert session.grants == (
        Grant("open.test"),
        Grant("mcp.test", credential=True, path="/team/mcp"),
        Grant("root.test", credential=True, path=""),
    )


def test_read_file_marks_a_file_it_cuts_so_the_agent_does_not_write_back_a_part(tmp_path: Path) -> None:
    """Say where and how much ``read_file`` cut, since ``write_file`` replaces the whole file (SKY-830).

    Args:
        tmp_path: Pytest's temporary folder.
    """
    size = shinka_evolve._TOOL_OUTPUT_CHARS
    files = {"long.py": b"a" * size + b"tail", "short.py": b"b" * size}
    options = _agent_options()
    ctx = _box_context(options, tmp_path, read_file=lambda tree, path: files.get(path))
    session = shinka_evolve._AgentSession(ctx, options, [])

    long_text, _ = session._tool(_call(0, "read_file", path="long.py"), "tree")
    short_text, _ = session._tool(_call(1, "read_file", path="short.py"), "tree")

    assert long_text == "a" * size + f"\n[cut at {size} of {size + 4} characters]"
    assert short_text == "b" * size


def test_list_files_cuts_a_long_list_at_whole_lines_with_a_note(tmp_path: Path) -> None:
    """Cut a file list over the output limit at a whole line, and say how many files it kept (SKY-830).

    Args:
        tmp_path: Pytest's temporary folder.
    """
    size = shinka_evolve._TOOL_OUTPUT_CHARS
    listed = [f"src/file_{index:05d}.py" for index in range(size // 10)]
    options = _agent_options()
    ctx = _box_context(options, tmp_path, list_files=lambda tree, path: listed)
    session = shinka_evolve._AgentSession(ctx, options, [])

    text, _ = session._tool(_call(0, "list_files", path=""), "tree")

    body, note = text.rsplit("\n", 1)
    kept = body.split("\n")
    assert len(body) <= size
    assert kept == listed[: len(kept)]
    assert len(kept) < len(listed)
    assert note == f"[cut: {len(kept)} of {len(listed)} files; use the run tool, for example find]"


def test_the_agent_run_tool_keeps_the_exit_code_and_the_output_ends_when_it_cuts(tmp_path: Path) -> None:
    """Keep the exit code line and the end of each stream when a ``run`` output is over the limit (SKY-830).

    Args:
        tmp_path: Pytest's temporary folder.
    """
    size = shinka_evolve._TOOL_OUTPUT_CHARS
    stdout = "s" * (2 * size) + "stdout end"

    def run_command(tree: str, script: str, grants: object) -> tuple[SimpleNamespace, str]:
        """Fail with a long stdout.

        Args:
            tree: The session's tree.
            script: The command.
            grants: The box's grants.

        Returns:
            A failed result and the tree after the command.
        """
        return SimpleNamespace(timed_out=False, exit_code=3, stdout=stdout, stderr="the error"), "after"

    options = _agent_options()
    session = shinka_evolve._AgentSession(_box_context(options, tmp_path, run_command=run_command), options, [])

    text, _ = session._tool(_call(0, "run", command="true"), "tree")

    assert len(text) <= size
    assert text.startswith("exit code 3\nstdout:\ns")
    assert "stdout end\nstderr:\nthe error\n" in text
    kept = text.split("\nstdout:\n", 1)[1].split("\nstderr:\n", 1)[0]
    assert text.endswith(
        f"[cut: kept the last {len(kept)} of {len(stdout)} stdout characters and the last 9 of 9 stderr characters]"
    )


def test_the_agent_run_tool_keeps_the_end_of_a_long_stderr_next_to_a_long_stdout(tmp_path: Path) -> None:
    """Keep the end of both streams, with at least half the room for stderr, when both are long (SKY-830).

    Args:
        tmp_path: Pytest's temporary folder.
    """
    size = shinka_evolve._TOOL_OUTPUT_CHARS
    stdout = "s" * (2 * size) + "stdout end"
    stderr = "e" * (2 * size) + "stderr end"

    def run_command(tree: str, script: str, grants: object) -> tuple[SimpleNamespace, str]:
        """Fail with a long stdout and a long stderr.

        Args:
            tree: The session's tree.
            script: The command.
            grants: The box's grants.

        Returns:
            A failed result and the tree after the command.
        """
        return SimpleNamespace(timed_out=False, exit_code=1, stdout=stdout, stderr=stderr), "after"

    options = _agent_options()
    session = shinka_evolve._AgentSession(_box_context(options, tmp_path, run_command=run_command), options, [])

    text, _ = session._tool(_call(0, "run", command="true"), "tree")

    out_kept, rest = text.split("\nstdout:\n", 1)[1].split("\nstderr:\n", 1)
    err_kept = rest.rsplit("\n[cut: ", 1)[0]
    assert len(text) <= size
    assert out_kept.endswith("stdout end")
    assert err_kept.endswith("stderr end")
    assert len(err_kept) >= (len(out_kept) + len(err_kept)) // 2


def test_the_agent_run_tool_says_a_command_timed_out_instead_of_an_exit_code(tmp_path: Path) -> None:
    """Report a timed-out command as timed out, since its exit code is not the command's own (SKY-830).

    Args:
        tmp_path: Pytest's temporary folder.
    """

    def run_command(tree: str, script: str, grants: object) -> tuple[SimpleNamespace, str]:
        """Time out.

        Args:
            tree: The session's tree.
            script: The command.
            grants: The box's grants.

        Returns:
            A timed-out result and the tree after the command.
        """
        return SimpleNamespace(timed_out=True, exit_code=124, stdout="partial", stderr=""), "after"

    options = _agent_options()
    session = shinka_evolve._AgentSession(_box_context(options, tmp_path, run_command=run_command), options, [])

    text, _ = session._tool(_call(0, "run", command="sleep 999"), "tree")

    assert text.split("\n", 1)[0] == "timed out"
    assert "exit code" not in text


def test_an_agent_run_call_whose_grants_do_not_resolve_stops_the_run(tmp_path: Path) -> None:
    """Stop the run, not the mutation, when the loop cannot resolve the box's grants (SKY-830).

    Args:
        tmp_path: Pytest's temporary folder.
    """

    def refuse(*args: object) -> None:
        """Refuse the grants as the loop does.

        Args:
            args: The command's tree, script and grants.

        Raises:
            UnknownCapabilityError: Always.
        """
        raise UnknownCapabilityError("mcp.test")

    options = _agent_options()
    ctx = _box_context(
        options,
        tmp_path,
        run_command=refuse,
        call_model=lambda *a: ModelReply("", Decimal(1), [_call(0, "run", command="true")]),  # type: ignore[list-item]
    )

    with pytest.raises(UnknownCapabilityError):
        shinka_evolve._AgentSession(ctx, options, []).edit({}, "task", lambda cents: None)
