"""Tests for GEPA's parallel batch scoring: the concurrency bound, order, attempt keys and stops (SKY-429)."""

from __future__ import annotations

import contextvars
import re
import threading
import time
from pathlib import Path
from typing import Any

import pytest

from core.models.blackbox import BLACKBOX_HARNESS_CUSTOM, BLACKBOX_TARGET_AGENT, BlackboxTarget
from core.sandbox.errors import SandboxInfraError

from ...budget_stop import BudgetReached
from .. import gepa_engine as gepa_mod
from .. import workspace_scorer as workspace_scorer_mod
from ..agent_eval import SandboxAgentScorer
from ..gepa_engine import GepaEngine
from ..harness import GatewayConfig
from ..protocol import EvalServer, Task
from ..sandbox import CommandResult
from .mocks import FakeSandboxRuntime, FakeSandboxSession, make_ctx, vowel_scorer

_GATEWAY = GatewayConfig(url="https://gw.example/v1", api_key="secret-key")
_ATTEMPT_ONE_KEY = re.compile(r"^agent-run:[0-9a-f]{32}:attempt-1$")
_CALLER_TAG: contextvars.ContextVar[str | None] = contextvars.ContextVar("caller_tag", default=None)


def _agent_target() -> BlackboxTarget:
    """Build a custom agent target that runs ``run-agent``.

    Returns:
        The target.
    """
    return BlackboxTarget(
        kind=BLACKBOX_TARGET_AGENT, harness=BLACKBOX_HARNESS_CUSTOM, model="m", run_command="run-agent"
    )


def _agent_scorer(runtime: FakeSandboxRuntime, scorer: Any = None) -> SandboxAgentScorer:
    """Wrap ``scorer`` in a sandbox agent scorer over ``runtime`` with the default no-retry policy.

    Args:
        runtime: The fake sandbox runtime.
        scorer: The user's scorer; a constant one when unset.

    Returns:
        The agent scorer.
    """
    scorer = scorer or (lambda candidate, case: (0.5, {"feedback": "ok"}))
    return SandboxAgentScorer(scorer, runtime=runtime, target=_agent_target(), gateway=_GATEWAY, job_id="job-1")


def _pairs(count: int) -> list[tuple[str, dict[str, int]]]:
    """Build one GEPA batch of ``count`` pairs on the same version.

    Args:
        count: Number of cases.

    Returns:
        The ``(candidate, case)`` pairs.
    """
    return [("hello world", {"i": index}) for index in range(count)]


def _run_batch(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
    server: EvalServer,
    pairs: list[tuple[Any, Any]],
    **ctx_overrides: Any,
) -> tuple[list[tuple[float, dict[str, Any]]], Any]:
    """Run the GEPA engine with a fake ``optimize_anything`` that scores one batch.

    Args:
        monkeypatch: Pytest monkeypatch fixture.
        tmp_path: Run directory.
        server: The budgeted scorer.
        pairs: The batch to send through ``batch_evaluator``.
        **ctx_overrides: Extra ``EngineContext`` fields, such as ``concurrency``.

    Returns:
        The batch results and the engine's result.
    """
    captured: dict[str, Any] = {}

    def fake_optimize_anything(**kwargs: Any) -> None:
        """Score one batch the way GEPA would, then report no result.

        Args:
            **kwargs: The engine's ``optimize_anything`` arguments.
        """
        token = _CALLER_TAG.set("caller")
        try:
            captured["results"] = kwargs["batch_evaluator"](pairs)
        finally:
            _CALLER_TAG.reset(token)

    monkeypatch.setattr(gepa_mod, "optimize_anything", fake_optimize_anything)
    task = Task(seed_candidate="hello world", objective="more vowels", cases=[case for _, case in pairs])
    result = GepaEngine().run(task, server, make_ctx(str(tmp_path), **ctx_overrides))
    return captured["results"], result


class _InFlightScorer:
    """Scorer that records how many calls overlap and holds each call until ``limit`` calls overlap.

    Args:
        limit: The overlap each call waits for before it returns.
        wait_seconds: The longest time one call waits for that overlap.
    """

    def __init__(self, limit: int, wait_seconds: float = 2.0) -> None:
        """Create the scorer."""
        self.limit = limit
        self.wait_seconds = wait_seconds
        self.in_flight = 0
        self.max_in_flight = 0
        self.calls = 0
        self.threads: set[int] = set()
        self._condition = threading.Condition()

    def __call__(self, candidate: Any, case: Any = None) -> tuple[float, dict[str, Any]]:
        """Count this call while it runs, and score the case by its index.

        Args:
            candidate: The version.
            case: The case.

        Returns:
            The case index as a score, and side information.
        """
        with self._condition:
            self.in_flight += 1
            self.calls += 1
            self.threads.add(threading.get_ident())
            self.max_in_flight = max(self.max_in_flight, self.in_flight)
            self._condition.notify_all()
            self._condition.wait_for(lambda: self.in_flight >= self.limit, timeout=self.wait_seconds)
            self.in_flight -= 1
        return float(case["i"]), {"feedback": f"case {case['i']}"}


def test_batch_calls_overlap_up_to_the_concurrency(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
    """A batch of agent cases runs ``ctx.concurrency`` calls at the same time.

    Args:
        monkeypatch: Pytest monkeypatch fixture.
        tmp_path: Run directory.
    """
    scorer = _InFlightScorer(limit=3)
    server = EvalServer(scorer, max_evals=16)

    _run_batch(monkeypatch, tmp_path, server, _pairs(3), concurrency=3)

    assert scorer.max_in_flight == 3
    assert scorer.calls == 3
    assert server.used == 3


def test_a_batch_larger_than_the_limit_never_goes_over_it(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
    """Six cases at concurrency two overlap two at a time and never three.

    Args:
        monkeypatch: Pytest monkeypatch fixture.
        tmp_path: Run directory.
    """
    scorer = _InFlightScorer(limit=3, wait_seconds=0.05)
    server = EvalServer(scorer, max_evals=16)

    results, _ = _run_batch(monkeypatch, tmp_path, server, _pairs(6), concurrency=2)

    assert scorer.max_in_flight == 2
    assert scorer.calls == 6
    assert [score for score, _ in results] == [0.0, 1.0, 2.0, 3.0, 4.0, 5.0]


def test_the_process_pool_limit_caps_a_wider_concurrency(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
    """A pool limit below the target concurrency holds the batch to the pool limit.

    Args:
        monkeypatch: Pytest monkeypatch fixture.
        tmp_path: Run directory.
    """
    monkeypatch.setattr(workspace_scorer_mod, "POOL_BOX_LIMIT", 2)
    monkeypatch.setattr(workspace_scorer_mod, "_pool_state", None)
    scorer = _InFlightScorer(limit=4, wait_seconds=0.05)
    server = EvalServer(scorer, max_evals=16)

    results, _ = _run_batch(monkeypatch, tmp_path, server, _pairs(8), concurrency=4)

    assert scorer.max_in_flight == 2
    assert scorer.calls == 8
    assert [score for score, _ in results] == [float(index) for index in range(8)]


def test_concurrency_one_keeps_the_serial_loop(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
    """The default concurrency scores every case on the caller thread, one at a time.

    Args:
        monkeypatch: Pytest monkeypatch fixture.
        tmp_path: Run directory.
    """
    scorer = _InFlightScorer(limit=2, wait_seconds=0.01)
    server = EvalServer(scorer, max_evals=16)

    _run_batch(monkeypatch, tmp_path, server, _pairs(4))

    assert scorer.max_in_flight == 1
    assert scorer.threads == {threading.get_ident()}


def test_parallel_results_keep_the_pair_order(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
    """Calls that finish out of order still return their results in pair order, as the serial loop does.

    Args:
        monkeypatch: Pytest monkeypatch fixture.
        tmp_path: Run directory.
    """
    finished: list[int] = []
    lock = threading.Lock()

    def slow_first(candidate: Any, case: Any = None) -> tuple[float, dict[str, Any]]:
        """Make earlier cases finish later, then score the case by its index.

        Args:
            candidate: The version.
            case: The case.

        Returns:
            The case index as a score, and side information.
        """
        time.sleep(0.02 * (4 - case["i"]))
        with lock:
            finished.append(case["i"])
        return float(case["i"]), {"feedback": f"case {case['i']}"}

    serial, _ = _run_batch(monkeypatch, tmp_path / "serial", EvalServer(slow_first, max_evals=16), _pairs(4))
    finished.clear()
    parallel, _ = _run_batch(
        monkeypatch, tmp_path / "parallel", EvalServer(slow_first, max_evals=16), _pairs(4), concurrency=4
    )

    assert finished != sorted(finished)
    assert parallel == serial
    assert [score for score, _ in parallel] == [0.0, 1.0, 2.0, 3.0]


def test_parallel_gepa_run_matches_the_serial_run(tmp_path: Path) -> None:
    """A real GEPA run picks the same version, score and tree at concurrency one and three.

    Args:
        tmp_path: Run directory.
    """
    cases = [{"i": 0}, {"i": 1}, {"i": 2}]
    results = []
    for concurrency in (1, 3):
        server = EvalServer(vowel_scorer, max_evals=16)
        task = Task(seed_candidate="hello world", objective="more vowels", cases=cases)
        ctx = make_ctx(str(tmp_path / str(concurrency)), concurrency=concurrency)
        results.append((GepaEngine().run(task, server, ctx), server.used))

    (serial, serial_used), (parallel, parallel_used) = results
    assert parallel.best_candidate == serial.best_candidate
    assert parallel.best_score == serial.best_score
    assert parallel.metadata["candidate_tree"] == serial.metadata["candidate_tree"]
    assert parallel_used == serial_used


def test_worker_threads_see_the_callers_context(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
    """A context value the caller sets reaches every parallel call.

    Args:
        monkeypatch: Pytest monkeypatch fixture.
        tmp_path: Run directory.
    """
    seen: list[str | None] = []

    def tagged(candidate: Any, case: Any = None) -> tuple[float, dict[str, Any]]:
        """Record the caller's context value.

        Args:
            candidate: The version.
            case: The case.

        Returns:
            A constant score and side information.
        """
        seen.append(_CALLER_TAG.get())
        return 0.5, {"feedback": "ok"}

    _run_batch(monkeypatch, tmp_path, EvalServer(tagged, max_evals=16), _pairs(4), concurrency=2)

    assert seen == ["caller"] * 4


def test_each_parallel_call_has_its_own_attempt_key(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
    """Every agent case opens one box under its own ``agent-run:<uuid>:attempt-1`` reservation key.

    Args:
        monkeypatch: Pytest monkeypatch fixture.
        tmp_path: Run directory.
    """
    runtime = FakeSandboxRuntime()
    server = EvalServer(_agent_scorer(runtime), max_evals=16)

    results, _ = _run_batch(monkeypatch, tmp_path, server, _pairs(4), concurrency=2)

    keys = [spec.operation_key for spec in runtime.specs]
    assert len(keys) == 4
    assert len(set(keys)) == 4
    assert all(_ATTEMPT_ONE_KEY.match(key) for key in keys)
    assert all(session.closed for session in runtime.sessions)
    assert [score for score, _ in results] == [0.5] * 4


def test_a_failed_parallel_call_is_not_retried(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
    """A box that dies is a metric error after one attempt, and its box is closed.

    Args:
        monkeypatch: Pytest monkeypatch fixture.
        tmp_path: Run directory.
    """

    def dying(command: str) -> CommandResult:
        """Kill the agent command; other commands succeed.

        Args:
            command: The shell command line.

        Returns:
            A clean exit for every command but the agent.

        Raises:
            SandboxInfraError: For the agent command.
        """
        if command == "run-agent":
            raise SandboxInfraError("host recycled")
        return CommandResult(exit_code=0)

    runtime = FakeSandboxRuntime(session_factory=lambda: FakeSandboxSession(script=dying))
    server = EvalServer(_agent_scorer(runtime), max_evals=16)

    results, _ = _run_batch(monkeypatch, tmp_path, server, _pairs(3), concurrency=3)

    assert len(runtime.sessions) == 3
    assert all(_ATTEMPT_ONE_KEY.match(spec.operation_key) for spec in runtime.specs)
    assert all(session.closed for session in runtime.sessions)
    assert all(info.get("status") == "metric_error" for _, info in results)


def test_a_budget_stop_starts_no_queued_call_and_leaks_no_box(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
    """A budget stop in one call ends the batch: queued cases never open a box, open boxes close.

    Args:
        monkeypatch: Pytest monkeypatch fixture.
        tmp_path: Run directory.
    """
    stop = threading.Event()

    def stopping(candidate: Any, case: Any = None) -> tuple[float, dict[str, Any]]:
        """Ask for a stop once the first case is scored.

        Args:
            candidate: The version.
            case: The agent's record.

        Returns:
            A constant score and side information.
        """
        stop.set()
        return 0.5, {"feedback": "ok"}

    def check_budget() -> None:
        """Raise a budget stop once a case asked for one.

        Raises:
            BudgetReached: After the first scored case.
        """
        if stop.is_set():
            raise BudgetReached

    runtime = FakeSandboxRuntime()
    server = EvalServer(_agent_scorer(runtime, stopping), max_evals=16)

    with pytest.raises(BudgetReached):
        _run_batch(monkeypatch, tmp_path, server, _pairs(6), concurrency=2, check_budget=check_budget)

    assert 1 <= len(runtime.sessions) <= 2
    assert all(session.closed for session in runtime.sessions)


def test_an_exhausted_server_ends_the_parallel_batch(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
    """When the scorer-run budget runs out mid-batch, the run returns the seed and no box leaks.

    Args:
        monkeypatch: Pytest monkeypatch fixture.
        tmp_path: Run directory.
    """
    runtime = FakeSandboxRuntime()
    server = EvalServer(_agent_scorer(runtime), max_evals=2)
    captured: dict[str, Any] = {}

    def fake_optimize_anything(**kwargs: Any) -> None:
        """Send a batch larger than the scorer-run budget.

        Args:
            **kwargs: The engine's ``optimize_anything`` arguments.
        """
        captured["results"] = kwargs["batch_evaluator"](_pairs(6))

    monkeypatch.setattr(gepa_mod, "optimize_anything", fake_optimize_anything)
    task = Task(seed_candidate="hello world", objective="more vowels", cases=[case for _, case in _pairs(6)])

    result = GepaEngine().run(task, server, make_ctx(str(tmp_path), concurrency=2))

    assert "results" not in captured
    assert result.best_candidate == "hello world"
    assert server.used == 2
    assert len(runtime.sessions) == 2
    assert all(session.closed for session in runtime.sessions)
