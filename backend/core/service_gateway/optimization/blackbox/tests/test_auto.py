"""Compare platform composition with the pinned upstream scheduling helpers."""

from __future__ import annotations

import json
import threading
from collections import defaultdict
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path
from typing import Any

import pytest
from gepa.oa.config import OptimizeAnythingConfig
from gepa.oa.engine import Result as UpstreamResult
from gepa.oa.ensemble import optimize_best_of

from core.billing.usage_tags import current_tags
from core.exceptions import ServiceError
from core.models.blackbox import BlackboxLaneResult, BlackboxStrategy

from ...budget_stop import BudgetReached
from .. import auto
from ..harness import GatewayConfig
from ..native_runtime import NativeOptions
from ..protocol import EngineContext, EvalServer, Result, ScorerAbortError, Task
from ..registry import EngineCapabilities
from ..upstream import AUTO_ENGINES, GEPA_SOURCE
from .mocks import make_ctx

CAPS = EngineCapabilities(proposer_available=True)


def test_auto_uncapped_byok_native_respects_remaining_budget(tmp_path: Path, fixture_engines: None) -> None:
    """Clamp an uncapped BYOK native lane to the remaining legacy budget."""
    native = NativeOptions(
        runtime="vercel",
        model="openai/test",
        gateway=GatewayConfig(url="https://example.com", api_key="key"),
        max_token_cost=None,
    )
    ctx = make_ctx(str(tmp_path), native_options=native, remaining_cost_usd=lambda: 0.25)
    server = EvalServer(score, max_evals=2 * (len(AUTO_ENGINES) + 1))
    result, _ = auto.run_strategy(BlackboxStrategy(), Task("seed"), server, ctx, caps=CAPS)
    assert result.best_candidate is not None


class FixtureEngine:
    """Preserve real upstream scheduling while making engine work deterministic."""

    def __init__(self, name: str) -> None:
        """Set the engine identifier.

        Args:
            name: Engine identifier.
        """
        self.name = name

    def run(self, task: Any, server: Any, ctx: EngineContext | None = None) -> Any:
        """Score the same fixture directly upstream or through the platform adapter.

        Args:
            task: Task with the scheduler's chosen seed.
            server: Upstream or platform evaluation allowance.
            ctx: Platform context when invoked through the adapter.

        Returns:
            The corresponding engine result with its aggregate score.
        """
        candidate = f"{task.seed_candidate}:{self.name}"
        remaining = server.remaining if ctx is not None else server.budget.remaining
        scores = [server.evaluate(candidate, {"i": i})[0] for i in range(remaining)]
        result_type = Result if ctx is not None else UpstreamResult
        return result_type(candidate, sum(scores) / len(scores), len(scores))

    def process_result(self, result: Any, output_dir: Any) -> None:
        """Leave fixture artifacts empty.

        Args:
            result: Completed result.
            output_dir: Artifact directory.
        """


def score(candidate: str, example: Any = None) -> tuple[float, dict[str, Any]]:
    """Rank fixture engines with negative scores to catch truthiness-based ranking.

    Args:
        candidate: Candidate containing the engine identifier.
        example: Ignored dataset example.

    Returns:
        Deterministic score and empty feedback.
    """
    return (-1.0 if "meta_harness" in candidate else -2.0), {}


@pytest.fixture(name="fixture_engines")
def _fixture_engines(monkeypatch: pytest.MonkeyPatch) -> None:
    """Replace engine work while preserving real upstream scheduling.

    Args:
        monkeypatch: Patch fixture.
    """
    monkeypatch.setattr(auto, "get_engine", lambda name, caps: FixtureEngine(name))


def test_auto_matches_direct_pinned_recipe(tmp_path: Path, fixture_engines: None) -> None:
    """Match upstream's per-engine winner and seed a fresh GEPA continuation.

    Args:
        tmp_path: Artifact directory.
        fixture_engines: Deterministic fixture.
    """
    direct = optimize_best_of(
        "seed",
        evaluator=score,
        configs=[
            OptimizeAnythingConfig(engine=FixtureEngine(name), max_evals=2, output_dir=tmp_path / f"direct-{name}")
            for name in AUTO_ENGINES
        ],
    )
    total_evals = 2 * (len(AUTO_ENGINES) + 1)
    server = EvalServer(score, max_evals=total_evals)
    result, lanes = auto.run_strategy(BlackboxStrategy(), Task("seed"), server, make_ctx(str(tmp_path)), caps=CAPS)
    assert result.best_candidate == f"{direct.best_candidate}:gepa"
    assert result.best_score == direct.best_score == -1.0
    assert server.used == result.total_evals == total_evals
    assert sorted(lane.engine for lane in lanes[: len(AUTO_ENGINES)]) == sorted(AUTO_ENGINES)
    assert [lane.phase for lane in lanes] == ["explore"] * len(AUTO_ENGINES) + ["continue"]
    assert result.metadata["upstream_source"] == GEPA_SOURCE
    assert result.metadata["upstream_recipe"] == "omni-gepa"
    assert "all_results" not in result.metadata


def test_single_uses_entire_budget(tmp_path: Path, fixture_engines: None) -> None:
    """Run exactly one requested engine without implicit exploration.

    Args:
        tmp_path: Artifact directory.
        fixture_engines: Deterministic fixture.
    """
    server = EvalServer(score, max_evals=5)
    result, lanes = auto.run_strategy(
        BlackboxStrategy(mode="single", engine="gepa"), Task("seed"), server, make_ctx(str(tmp_path)), caps=CAPS
    )
    assert result.best_candidate == "seed:gepa"
    assert server.used == 5
    assert len(lanes) == 1
    assert lanes[0].phase == "single"


def test_missing_recipe_engine_rejects_before_scoring(tmp_path: Path) -> None:
    """Never replace a missing native engine with Best-of-N or a GEPA-only run.

    Args:
        tmp_path: Artifact directory.
    """
    server = EvalServer(score, max_evals=8)
    with pytest.raises(ServiceError, match="not available"):
        auto.run_strategy(BlackboxStrategy(), Task("seed"), server, make_ctx(str(tmp_path)))
    assert server.used == 0


def test_auto_rejects_too_small_budget(tmp_path: Path, fixture_engines: None) -> None:
    """Reject before work when the per-lane and continuation shares cannot all fit.

    Args:
        tmp_path: Artifact directory.
        fixture_engines: Deterministic fixture.
    """
    with pytest.raises(ServiceError, match="scorer runs"):
        auto.run_strategy(
            BlackboxStrategy(),
            Task("seed"),
            EvalServer(score, max_evals=len(AUTO_ENGINES)),
            make_ctx(str(tmp_path)),
            caps=CAPS,
        )


def test_compositions_refuse_named_parts(tmp_path: Path) -> None:
    """Enforce upstream's string-only native proposer contract.

    Args:
        tmp_path: Artifact directory.
    """
    with pytest.raises(ServiceError, match="text starting point"):
        auto.run_strategy(
            BlackboxStrategy(),
            Task({"a": "seed"}),
            EvalServer(score, max_evals=8),
            make_ctx(str(tmp_path)),
            caps=CAPS,
        )


def test_engine_failure_never_falls_back_to_seed(tmp_path: Path, fixture_engines: None) -> None:
    """Propagate run-level failure without reporting a successful alternative run.

    Args:
        tmp_path: Artifact directory.
        fixture_engines: Deterministic fixture.
    """

    def fail(candidate: Any, example: Any = None) -> Any:
        """Stop the fixture at the scoring boundary.

        Args:
            candidate: Proposed candidate.
            example: Dataset example.
        """
        raise ScorerAbortError("scorer unavailable")

    with pytest.raises(ScorerAbortError, match="scorer unavailable"):
        auto.run_strategy(
            BlackboxStrategy(), Task("seed"), EvalServer(fail, max_evals=8), make_ctx(str(tmp_path)), caps=CAPS
        )


def test_auto_continuation_stops_after_cumulative_spend(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    """Refuse the continuation a fresh allowance once exploration spent the run's budget.

    Args:
        tmp_path: Artifact directory.
        monkeypatch: Patch fixture.
    """
    spent = [0.0]
    starts: list[str] = []

    class SpendingEngine(FixtureEngine):
        """Attach reconciled usage to the deterministic scoring fixture."""

        def run(self, task: Any, server: Any, ctx: EngineContext | None = None) -> Any:
            """Record completed proposer spend before the next slice is considered.

            Args:
                task: Scheduler-selected task.
                server: Scoring allowance.
                ctx: Run accounting context.

            Returns:
                Deterministic candidate result.
            """
            starts.append(self.name)
            result = super().run(task, server, ctx)
            spent[0] += 4.0
            return result

    monkeypatch.setattr(auto, "get_engine", lambda name, caps: SpendingEngine(name))
    # Fund exactly one 4.0 slice per exploration lane so the run's budget is spent
    # before the continuation, whatever engines the recipe explores.
    budget = 4.0 * len(AUTO_ENGINES)
    context = make_ctx(
        str(tmp_path), remaining_cost_usd=lambda: max(0.0, budget - spent[0]), proposer_token_budget_usd=budget
    )
    with pytest.raises(BudgetReached, match="budget") as stopped:
        auto.run_strategy(
            BlackboxStrategy(),
            Task("seed"),
            EvalServer(score, max_evals=100),
            context,
            caps=CAPS,
        )
    assert stopped.value.result.best_candidate == "seed:meta_harness"
    assert stopped.value.result.best_score == -1.0
    assert stopped.value.evidence["selection_scope"] == "validation"
    assert spent[0] == budget
    assert sorted(starts) == sorted(AUTO_ENGINES)


@pytest.mark.parametrize("cases", [[], [{"i": 0}], [{"i": 0}, {"i": 1}, {"i": 2}]])
def test_auto_runs_with_zero_one_or_many_cases(tmp_path: Path, fixture_engines: None, cases: list[Any]) -> None:
    """Auto needs no cases: every lane and the continuation run with or without them.

    Args:
        tmp_path: Artifact directory.
        fixture_engines: Deterministic fixture.
        cases: The run's cases.
    """
    server = EvalServer(score, max_evals=2 * (len(AUTO_ENGINES) + 1))

    result, lanes = auto.run_strategy(
        BlackboxStrategy(), Task("seed", cases=cases), server, make_ctx(str(tmp_path)), caps=CAPS
    )

    assert result.best_candidate == "seed:meta_harness:gepa"
    assert all(lane.status != "failed" for lane in lanes)


def test_a_failed_auto_lane_does_not_sink_the_run(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    """One engine crashing is recorded on its lane while the others' work still wins.

    Args:
        tmp_path: Artifact directory.
        monkeypatch: Patch fixture.
    """

    class CrashingEngine(FixtureEngine):
        """Crash the way a broken engine does, before scoring anything."""

        def run(self, task: Any, server: Any, ctx: EngineContext | None = None) -> Any:
            """Raise an engine-internal error.

            Args:
                task: Scheduler-selected task.
                server: Scoring allowance.
                ctx: Run accounting context.

            Raises:
                RuntimeError: Always.
            """
            raise RuntimeError("engine crashed")

    monkeypatch.setattr(
        auto, "get_engine", lambda name, caps: CrashingEngine(name) if name == "autoresearch" else FixtureEngine(name)
    )
    server = EvalServer(score, max_evals=2 * (len(AUTO_ENGINES) + 1))

    result, lanes = auto.run_strategy(BlackboxStrategy(), Task("seed"), server, make_ctx(str(tmp_path)), caps=CAPS)

    assert result.best_candidate == "seed:meta_harness:gepa"
    [failed] = [lane for lane in lanes if lane.status == "failed"]
    assert failed.engine == "autoresearch"
    assert "engine crashed" in (failed.error or "")


def test_auto_fails_when_every_lane_fails(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    """With nothing to continue from, the run reports every lane's error.

    Args:
        tmp_path: Artifact directory.
        monkeypatch: Patch fixture.
    """

    class CrashingEngine(FixtureEngine):
        """Crash before scoring anything."""

        def run(self, task: Any, server: Any, ctx: EngineContext | None = None) -> Any:
            """Raise an engine-internal error.

            Args:
                task: Scheduler-selected task.
                server: Scoring allowance.
                ctx: Run accounting context.

            Raises:
                RuntimeError: Always.
            """
            raise RuntimeError("engine crashed")

    monkeypatch.setattr(auto, "get_engine", lambda name, caps: CrashingEngine(name))

    with pytest.raises(ServiceError, match="Every Auto exploration lane failed"):
        auto.run_strategy(
            BlackboxStrategy(),
            Task("seed"),
            EvalServer(score, max_evals=2 * (len(AUTO_ENGINES) + 1)),
            make_ctx(str(tmp_path)),
            caps=CAPS,
        )


class BilledFixtureEngine(FixtureEngine):
    """Bill model calls on the lane's own thread and on a worker pool, like real engines."""

    def __init__(self, name: str, ledger: list[tuple[dict[str, str], float]], lock: threading.Lock) -> None:
        """Share one billing ledger across every lane.

        Args:
            name: Engine identifier.
            ledger: Recorded ``(tags, cost)`` per billed call.
            lock: Guards the ledger across lane threads.
        """
        super().__init__(name)
        self.ledger, self.lock = ledger, lock

    def bill(self, cost: float) -> None:
        """Record one billed call under the tags the gateway would read.

        Args:
            cost: Call cost in USD.
        """
        with self.lock:
            self.ledger.append((current_tags(), cost))

    def run(self, task: Any, server: Any, ctx: EngineContext | None = None) -> Any:
        """Bill a reflection call in-thread and a mutation call from a child thread pool.

        Args:
            task: Task with the scheduler's chosen seed.
            server: Platform evaluation allowance.
            ctx: Platform context.

        Returns:
            The fixture engine result.
        """
        self.bill(0.25)
        with ThreadPoolExecutor(max_workers=2) as pool:
            pool.submit(self.bill, 0.5).result()
        return super().run(task, server, ctx)


def test_auto_tags_every_billed_call_with_its_lane(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    """Every billed call inside an Auto lane carries ``lane=<phase>-<engine>``, including worker threads.

    Args:
        tmp_path: Artifact directory.
        monkeypatch: Patch fixture.
    """
    ledger: list[tuple[dict[str, str], float]] = []
    lock = threading.Lock()
    monkeypatch.setattr(auto, "get_engine", lambda name, caps: BilledFixtureEngine(name, ledger, lock))

    def billed_score(candidate: str, example: Any = None) -> tuple[float, dict[str, Any]]:
        """Bill the scorer's model call, then score as the plain fixture does.

        Args:
            candidate: Candidate containing the engine identifier.
            example: Ignored dataset example.

        Returns:
            Deterministic score and empty feedback.
        """
        with lock:
            ledger.append((current_tags(), 0.125))
        return score(candidate, example)

    server = EvalServer(billed_score, max_evals=2 * (len(AUTO_ENGINES) + 1))
    auto.run_strategy(BlackboxStrategy(), Task("seed"), server, make_ctx(str(tmp_path)), caps=CAPS)

    expected = {auto.lane_tag("explore", name) for name in AUTO_ENGINES} | {auto.lane_tag("continue", "gepa")}
    assert {tags.get("lane") for tags, _ in ledger} == expected
    by_lane: dict[str, float] = defaultdict(float)
    for tags, cost in ledger:
        by_lane[tags["lane"]] += cost
    assert sum(by_lane.values()) == pytest.approx(sum(cost for _, cost in ledger))
    assert all(by_lane[lane] > 0 for lane in expected)
    assert current_tags() == {}


def test_single_engine_calls_carry_no_lane(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    """A single-engine run is not an Auto lane, so its calls stay untagged by lane.

    Args:
        tmp_path: Artifact directory.
        monkeypatch: Patch fixture.
    """
    ledger: list[tuple[dict[str, str], float]] = []
    monkeypatch.setattr(auto, "get_engine", lambda name, caps: BilledFixtureEngine(name, ledger, threading.Lock()))
    server = EvalServer(score, max_evals=3)
    auto.run_strategy(
        BlackboxStrategy(mode="single", engine="gepa"), Task("seed"), server, make_ctx(str(tmp_path)), caps=CAPS
    )
    assert ledger
    assert all("lane" not in tags for tags, _ in ledger)


def test_auto_budget_stop_evidence_keeps_a_crashed_lane_out_of_strict_json(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """A lane that hands off the seed at -inf stores no score, so the stop evidence fits JSONB (SKY-367).

    Args:
        tmp_path: Artifact directory.
        monkeypatch: Patch fixture.
    """
    crashed, stopping = AUTO_ENGINES[0], AUTO_ENGINES[-1]

    class StoppingEngine(FixtureEngine):
        """Rank by the mean of the scored cases, and stop the budget in one lane."""

        def run(self, task: Any, server: Any, ctx: EngineContext | None = None) -> Any:
            """Score each case, then return the version or stop the budget.

            Args:
                task: Scheduler-selected task.
                server: Scoring allowance.
                ctx: Run accounting context.

            Returns:
                The version at the mean of its scored cases, as an engine that fills null scores would.

            Raises:
                BudgetReached: In the stopping lane.
            """
            candidate = f"{task.seed_candidate}:{self.name}"
            scores = [server.evaluate(candidate, {"i": i})[0] for i in range(2)]
            if self.name == stopping:
                raise BudgetReached("budget")
            scored = [value for value in scores if value is not None]
            return Result(candidate, sum(scored) / len(scored), len(scores))

    def crash_one_lane(candidate: str, example: Any = None) -> tuple[float, dict[str, Any]]:
        """Raise on case 1 of the crashed lane's version, else score as the fixture does.

        Args:
            candidate: Candidate containing the engine identifier.
            example: Dataset example.

        Returns:
            Deterministic score and empty feedback.

        Raises:
            RuntimeError: On case 1 of the crashed lane's version.
        """
        if crashed in candidate and example == {"i": 1}:
            raise RuntimeError("scorer bug")
        return score(candidate, example)

    monkeypatch.setattr(auto, "get_engine", lambda name, caps: StoppingEngine(name))
    with pytest.raises(BudgetReached) as stopped:
        auto.run_strategy(
            BlackboxStrategy(),
            Task("seed"),
            EvalServer(crash_one_lane, max_evals=100),
            make_ctx(str(tmp_path)),
            caps=CAPS,
        )

    evidence = stopped.value.evidence
    lanes = evidence["_lanes"]
    stored = [
        BlackboxLaneResult(engine=lane.engine, phase=lane.phase, status=lane.status, best_score=lane.best_score)
        for lane in lanes
    ]
    json.dumps([row.model_dump(mode="json") for row in stored], allow_nan=False)
    json.dumps(evidence["completed_lanes"], allow_nan=False)
    assert next(lane for lane in lanes if lane.engine == crashed).best_score is None
    assert crashed not in {row["engine"] for row in evidence["completed_lanes"]}
    assert evidence["completed_lanes"]
