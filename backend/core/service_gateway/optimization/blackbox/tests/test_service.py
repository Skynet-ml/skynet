"""End-to-end tests for the job entry points with a fake reflection LM."""

from __future__ import annotations

import logging
import threading
import time
from pathlib import Path
from typing import Any

import pytest

from core.api.errors import DomainError
from core.billing.signals import BudgetReached
from core.constants import (
    DETAIL_BASELINE,
    DETAIL_OPTIMIZED,
    PROGRESS_BASELINE,
    PROGRESS_EVALUATION_STARTED,
    PROGRESS_LANE_COMPLETED,
    PROGRESS_LANE_HANDOFF,
    PROGRESS_LANE_STARTED,
    PROGRESS_OPTIMIZED,
    PROGRESS_OPTIMIZER,
    TQDM_N_KEY,
    TQDM_TOTAL_KEY,
)
from core.exceptions import ServiceError
from core.models.blackbox import (
    BLACKBOX_ENGINE_BEST_OF_N,
    BLACKBOX_ENGINE_GEPA,
    BlackboxRunRequest,
)
from core.models.results import ModelTokenUsage
from core.models.unified import UnifiedCase, UnifiedRunRequest

from .. import autoresearch as autoresearch_mod
from .. import autosaddler as autosaddler_mod
from .. import meta_harness as meta_harness_mod
from .. import scorer as scorer_mod
from .. import service as service_mod
from .. import shinka_evolve as shinka_evolve_mod
from ..agent_runs import PHASE_BASELINE, PHASE_FINAL
from ..auto import LaneOutcome
from ..harness import GatewayConfig
from ..native_runtime import NativeOptions
from ..protocol import (
    METRIC_ERROR,
    SEARCH_FLOOR,
    Candidate,
    EngineContext,
    EvalServer,
    Result,
    Task,
    candidate_key,
)
from ..sandbox import LocalSubprocessRuntime
from ..sandbox_scorer import ScorerGateway, ScorerUsage
from ..service import run_blackbox_optimization, validate_blackbox_payload
from ..workspace_scorer import build_workspace_scorer
from .mocks import (
    AGENT_OUTPUT_SCORER_CODE,
    VOWEL_SCORER_CODE,
    FakeGateway,
    FakeReflectionLM,
    FakeSandboxRuntime,
    vowel_scorer,
)
from .test_reward import GEOCODE_METRIC

_CASES = [{"target": "aeiou", "i": i} for i in range(10)]


def test_byok_novelty_requires_embedding_provider() -> None:
    """Reject a BYOK novelty route whose provider cannot serve embeddings."""
    payload = _payload(
        strategy={"mode": "single", "engine": "shinka_evolve"},
        shinka={"novelty": True},
        max_cost_cents=100,
        reflection_model_config={"name": "anthropic/claude-sonnet-4", "token_source": "byok"},
    )
    with pytest.raises(DomainError) as error:
        validate_blackbox_payload(payload, verify_scorer=False)
    assert error.value.code == "submission.byok_novelty_unsupported"


def test_job_validation_rejects_saved_custom_base_as_service_error() -> None:
    """Report unsupported novelty through the service error contract."""
    payload = _payload(
        shinka={"novelty": True}, reflection_model_config={"name": "openai/model", "token_source": "byok"}
    )
    with pytest.raises(DomainError):
        service_mod.validate_job(payload, api_bases={"openai": "https://public.example/v1"})


class _RecordingScorer:
    """Wrap a job scorer and record every ``(candidate, case)`` it is asked to score."""

    def __init__(self, inner: Any) -> None:
        """Wrap ``inner``.

        Args:
            inner: The real job scorer.
        """
        self.inner = inner
        self.usage = inner.usage
        self.calls: list[tuple[Candidate, Any]] = []
        self._lock = threading.Lock()

    def __call__(self, candidate: Candidate, case: Any = None) -> tuple[float, dict[str, Any]]:
        """Record the call and score it with the wrapped scorer.

        Args:
            candidate: The version to score.
            case: The case, if any.

        Returns:
            The wrapped scorer's result.
        """
        with self._lock:
            self.calls.append((candidate, case))
        return self.inner(candidate, case)

    def close(self) -> None:
        """Close the wrapped scorer."""
        self.inner.close()


def _payload(**overrides: Any) -> BlackboxRunRequest:
    """Build a valid request around the vowel scorer.

    Args:
        **overrides: Fields to replace in the default payload.

    Returns:
        The validated request.
    """
    data: dict[str, Any] = {
        "seed_candidate": "hello world",
        "objective": "maximize vowel density",
        "scorer": {"kind": "python", "metric_code": VOWEL_SCORER_CODE},
        "cases": _CASES,
        "seed": 1,
        "budget": {"max_scorer_runs": 12},
        "strategy": {"mode": "single", "engine": BLACKBOX_ENGINE_BEST_OF_N},
        "reflection_model_config": {"name": "fake/model"},
    }
    data.update(overrides)
    return BlackboxRunRequest.model_validate(data)


@pytest.fixture
def fake_lm(monkeypatch: pytest.MonkeyPatch) -> FakeReflectionLM:
    """Swap the reflection model for the improving fake.

    Args:
        monkeypatch: Pytest fixture.

    Returns:
        The fake the service will use.
    """
    lm = FakeReflectionLM()
    monkeypatch.setattr(service_mod, "build_language_model", lambda config, *, disable_cache=False: lm)
    return lm


@pytest.fixture
def fake_native_proposers(monkeypatch: pytest.MonkeyPatch) -> list[tuple[str, EngineContext]]:
    """Replace paid native proposal generation while retaining upstream ensemble scheduling.

    Args:
        monkeypatch: Pytest fixture for native runtime and gateway replacements.

    Returns:
        Native invocations with the execution context received from the service.
    """
    invocations: list[tuple[str, EngineContext]] = []
    monkeypatch.setattr(service_mod, "native_runtime_unavailable_reason", lambda _runtime, _settings: None)
    monkeypatch.setattr(
        service_mod,
        "gateway_from_settings",
        lambda _settings: GatewayConfig(url="https://unused.example/v1", api_key="test-key"),
    )

    def run_native(engine: str, task: Task, server: EvalServer, ctx: EngineContext) -> Result:
        """Score a deterministic native proposal against the real budgeted evaluator.

        Args:
            engine: Native engine selected by the upstream composition.
            task: Actual slice task passed by upstream.
            server: Slice budget and scorer bridge.
            ctx: Model, runtime and proposer budget passed by the service.

        Returns:
            Candidate-level aggregate evidence from the fake proposal.
        """
        invocations.append((engine, ctx))
        candidate = "aeioua" if engine == "autoresearch" else "aeiouaa"
        scores = []
        for example in task.cases or [None]:
            if server.remaining <= 0:
                break
            score, _ = server.evaluate(candidate, example)
            scores.append(score)
        return Result(
            best_candidate=candidate,
            best_score=sum(scores) / len(scores) if scores else 1.0,
            total_evals=server.used,
            metadata={"engine": engine},
        )

    monkeypatch.setattr(autoresearch_mod, "run_native_engine", run_native)
    monkeypatch.setattr(meta_harness_mod, "run_native_engine", run_native)
    monkeypatch.setattr(autosaddler_mod, "run_native_engine", run_native)
    monkeypatch.setattr(shinka_evolve_mod, "run_native_engine", run_native)
    return invocations


def test_run_scores_baseline_and_best_in_a_separate_final_run(fake_lm: FakeReflectionLM, tmp_path: Path) -> None:
    """A single best_of_n run improves the seed and reports the final run, lanes, usage and reflection timing."""
    sink: list[tuple[str, dict[str, Any]]] = []

    response = run_blackbox_optimization(
        _payload(),
        artifact_id="job-1",
        progress_callback=lambda e, m: sink.append((e, m)),
        gepa_log_dir_path=str(tmp_path),
    )

    assert response.optimizer_name == BLACKBOX_ENGINE_BEST_OF_N
    assert response.engine_used == BLACKBOX_ENGINE_BEST_OF_N
    assert response.strategy_mode == "single"
    assert response.split_counts is not None
    assert (response.split_counts.train, response.split_counts.val, response.split_counts.test) == (7, 2, 1)
    assert response.case_count == 1
    assert response.final_scorer_runs == 2 * (2 + 1)
    assert response.baseline_val_score == pytest.approx(3 / 11)
    assert response.best_val_score == 1.0
    assert response.seed_candidate == "hello world"
    assert response.best_candidate == "aeiou"
    assert response.baseline_score == pytest.approx(3 / 11)
    assert response.best_score == 1.0
    assert response.metric_improvement == pytest.approx(1 - 3 / 11)
    assert response.regression_guard_applied is False
    assert response.total_scorer_runs <= 12
    assert [(lane.engine, lane.phase, lane.status) for lane in response.lanes] == [
        (BLACKBOX_ENGINE_BEST_OF_N, "single", "completed")
    ]
    assert response.num_lm_calls == len(fake_lm.history) > 0
    assert response.total_tokens == 15 * len(fake_lm.history)
    assert [(u.model, u.input_tokens, u.output_tokens) for u in response.usage_by_model] == [
        ("fake/model", 10 * len(fake_lm.history), 5 * len(fake_lm.history))
    ]
    assert response.lm_activity is not None
    assert response.lm_activity.generation == {}
    reflection_stats = response.lm_activity.reflection["training"]
    assert reflection_stats.calls == len(fake_lm.history)
    assert reflection_stats.avg_response_time_ms is not None
    assert response.optimization_metadata["budget"] == {
        "max_scorer_runs": 12,
        "stop_at_score": None,
        "max_iterations": None,
    }
    assert response.optimization_metadata["target"]["kind"] == "text"
    assert response.details["optimizer_best_score"] == 1.0
    assert response.details["n_samples"] == len(fake_lm.history)
    assert response.details["n_parse_failures"] == 0

    events = [event for event, _ in sink]
    assert events[-3:] == [PROGRESS_EVALUATION_STARTED, PROGRESS_BASELINE, PROGRESS_OPTIMIZED]
    assert PROGRESS_LANE_STARTED in events
    assert PROGRESS_LANE_COMPLETED in events
    assert PROGRESS_LANE_HANDOFF not in events
    baseline_event = next(m for e, m in sink if e == PROGRESS_BASELINE)
    assert baseline_event[DETAIL_BASELINE] == pytest.approx(3 / 11)
    optimized_event = next(m for e, m in sink if e == PROGRESS_OPTIMIZED)
    assert optimized_event[DETAIL_OPTIMIZED] == 1.0
    optimizer_events = [m for e, m in sink if e == PROGRESS_OPTIMIZER]
    assert optimizer_events
    assert optimizer_events[-1][TQDM_TOTAL_KEY] == 12
    assert optimizer_events[-1][TQDM_N_KEY] == response.total_scorer_runs


def test_auto_run_hands_off_between_engines(
    fake_lm: FakeReflectionLM, tmp_path: Path, fake_native_proposers: list[tuple[str, EngineContext]]
) -> None:
    """Run every upstream exploration lane before the GEPA continuation.

    Args:
        fake_lm: Metered model fake used by the real in-process implementations.
        tmp_path: Per-test artifact directory.
        fake_native_proposers: Captured native invocations without paid model calls.
    """
    sink: list[tuple[str, dict[str, Any]]] = []

    response = run_blackbox_optimization(
        _payload(strategy={"mode": "auto"}, budget={"max_scorer_runs": 24}, max_cost_cents=100),
        artifact_id="job-2",
        progress_callback=lambda e, m: sink.append((e, m)),
        gepa_log_dir_path=str(tmp_path),
    )

    assert response.optimizer_name == "auto"
    assert response.strategy_mode == "auto"
    assert {(lane.engine, lane.phase) for lane in response.lanes[:5]} == {
        ("gepa", "explore"),
        ("autoresearch", "explore"),
        ("meta_harness", "explore"),
        ("autosaddler", "explore"),
        ("shinka_evolve", "explore"),
    }
    assert (response.lanes[-1].engine, response.lanes[-1].phase) == ("gepa", "continue")
    assert response.engine_used == "gepa"
    assert {engine for engine, _ in fake_native_proposers} == {
        "autoresearch",
        "meta_harness",
        "autosaddler",
        "shinka_evolve",
    }
    shinka = next(ctx.native_options for engine, ctx in fake_native_proposers if engine == "shinka_evolve")
    assert [route["model"] for route in shinka.shinka_models] == ["fake/model"]
    assert shinka.shinka["patch_diff"] == pytest.approx(0.6)
    assert all(ctx.native_options.model == "fake/model" for _, ctx in fake_native_proposers)
    assert all(ctx.native_options.runtime == "vercel" for _, ctx in fake_native_proposers)
    assert all(ctx.native_options.max_token_cost > 0 for _, ctx in fake_native_proposers)
    assert response.total_scorer_runs <= 24
    assert response.best_score >= response.baseline_score
    handoffs = [m for e, m in sink if e == PROGRESS_LANE_HANDOFF]
    assert len(handoffs) == 1
    assert handoffs[0]["to_engine"] == "gepa"


def test_byok_reflection_budget_does_not_limit_model_calls(
    fake_lm: FakeReflectionLM, tmp_path: Path, fake_native_proposers: list[tuple[str, EngineContext]]
) -> None:
    """Allow BYOK reflection with a spending limit without a fee-based model cap."""
    run_blackbox_optimization(
        _payload(
            strategy={"mode": "auto"},
            budget={"max_scorer_runs": 24},
            max_cost_cents=100,
            execution_budget_id="fixture",
            reflection_model_config={"name": "fake/model", "token_source": "byok"},
        ),
        artifact_id="job-byok",
        gepa_log_dir_path=str(tmp_path),
    )
    assert fake_native_proposers
    assert all(ctx.native_options.max_token_cost is None for _, ctx in fake_native_proposers)


def test_native_proposers_receive_the_reflection_reasoning_effort(
    fake_lm: FakeReflectionLM, tmp_path: Path, fake_native_proposers: list[tuple[str, EngineContext]]
) -> None:
    """The effort chosen for the optimization model passes validation and reaches every native proposer."""
    run_blackbox_optimization(
        _payload(
            strategy={"mode": "auto"},
            budget={"max_scorer_runs": 24},
            max_cost_cents=100,
            reflection_model_config={"name": "fake/model", "extra": {"reasoning_effort": "high"}},
        ),
        artifact_id="job-effort",
        gepa_log_dir_path=str(tmp_path),
    )
    assert fake_native_proposers
    assert all(ctx.native_options.reasoning_effort == "high" for _, ctx in fake_native_proposers)


def test_native_proposers_keep_the_default_effort_without_a_choice(
    fake_lm: FakeReflectionLM, tmp_path: Path, fake_native_proposers: list[tuple[str, EngineContext]]
) -> None:
    """A run with no chosen effort hands the native proposers none."""
    run_blackbox_optimization(
        _payload(strategy={"mode": "auto"}, budget={"max_scorer_runs": 24}, max_cost_cents=100),
        artifact_id="job-no-effort",
        gepa_log_dir_path=str(tmp_path),
    )
    assert fake_native_proposers
    assert all(ctx.native_options.reasoning_effort is None for _, ctx in fake_native_proposers)


def test_legacy_auto_byok_reflection_with_cost_limit(
    fake_lm: FakeReflectionLM, tmp_path: Path, fake_native_proposers: list[tuple[str, EngineContext]]
) -> None:
    """Run Auto with uncapped BYOK reflection and a legacy cost limit."""
    payload = _payload(
        strategy={"mode": "auto"},
        budget={"max_scorer_runs": 24},
        max_cost_cents=100,
        reflection_model_config={"name": "fake/model", "token_source": "byok"},
    )
    assert not payload.execution_budget_id
    response = run_blackbox_optimization(payload, artifact_id="legacy-auto-byok", gepa_log_dir_path=str(tmp_path))
    assert response.strategy_mode == "auto"
    assert fake_native_proposers
    assert all(ctx.native_options.max_token_cost is not None for _, ctx in fake_native_proposers)


def test_legacy_byok_task_usage_does_not_consume_budget(
    fake_lm: FakeReflectionLM, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Keep a BYOK task model out of the legacy budget checks."""
    task_lm = FakeReflectionLM()
    original = service_mod.job_spec_from_payload

    def job_from_payload(payload: BlackboxRunRequest) -> service_mod.JobSpec:
        """Add a BYOK task model to the job."""
        job = original(payload)
        job.extra_lms = [task_lm]
        job.extra_lm_sources = ["byok"]
        return job

    def strategy(_strategy: Any, _task: Task, _server: EvalServer, ctx: EngineContext, *_args: Any, **_kwargs: Any):
        """Exercise both budget checks after task model usage."""
        task_lm.history.append({"usage": {"prompt_tokens": 1000000, "completion_tokens": 1000000}})
        service_mod.dspy.settings.callbacks[0]._check()
        ctx.check_budget()
        assert ctx.remaining_cost_usd() > 0
        return Result(best_candidate="hello world", best_score=0.5, total_evals=0), []

    monkeypatch.setattr(service_mod, "job_spec_from_payload", job_from_payload)
    monkeypatch.setattr(service_mod, "run_strategy", strategy)
    run_blackbox_optimization(_payload(max_cost_cents=1), artifact_id="byok-task", gepa_log_dir_path=str(tmp_path))


@pytest.mark.parametrize("source", ["byok", "managed"])
def test_legacy_judge_source_controls_budget(
    fake_lm: FakeReflectionLM, tmp_path: Path, monkeypatch: pytest.MonkeyPatch, source: str
) -> None:
    """Exclude only BYOK judge usage from the legacy ceiling."""
    usage = ScorerUsage("openai/gpt-4o-mini")

    class Scorer:
        """Provide a fake judge ledger."""

        def __init__(self) -> None:
            """Attach usage."""
            self.usage = usage

        def __call__(self, candidate: Candidate, case: Any = None) -> tuple[float, dict[str, Any]]:
            """Score without network access."""
            return vowel_scorer(candidate, case)

        def close(self) -> None:
            """Close the scorer."""

    def strategy(_strategy: Any, _task: Task, _server: EvalServer, ctx: EngineContext, *_args: Any, **_kwargs: Any):
        """Check the ceiling after judge usage."""
        usage.record([{"prompt_tokens": 2000000, "completion_tokens": 2000000}])
        if source == "byok":
            service_mod.dspy.settings.callbacks[0]._check()
            ctx.check_budget()
        else:
            with pytest.raises(BudgetReached):
                service_mod.dspy.settings.callbacks[0]._check()
            with pytest.raises(BudgetReached):
                ctx.check_budget()
        return Result(best_candidate="hello world", best_score=0.5, total_evals=0), []

    monkeypatch.setattr(service_mod, "build_scorer", lambda *args, **kwargs: Scorer())
    monkeypatch.setattr(service_mod, "run_strategy", strategy)
    run_blackbox_optimization(
        _payload(
            max_cost_cents=100,
            scorer={
                "kind": "python",
                "metric_code": VOWEL_SCORER_CODE,
                "model": {"name": "openai/gpt-4o-mini", "token_source": source},
            },
        ),
        artifact_id="judge",
        gepa_log_dir_path=str(tmp_path),
    )


def test_legacy_byok_reflection_keeps_managed_scorer_budget(
    fake_lm: FakeReflectionLM, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Exclude BYOK reflection while stopping managed scorer spend at the cap."""
    usage = ScorerUsage("openai/gpt-4o-mini")

    class ManagedScorer:
        """Supply a managed scorer ledger to the legacy run."""

        def __init__(self) -> None:
            """Bind the managed usage ledger."""
            self.usage = usage

        def __call__(self, candidate: Candidate, case: Any = None) -> tuple[float, dict[str, Any]]:
            """Score the candidate without a network call."""
            return vowel_scorer(candidate, case)

        def close(self) -> None:
            """Close the scorer."""

    def strategy(_strategy: Any, _task: Task, _server: EvalServer, ctx: EngineContext, *_args: Any, **_kwargs: Any):
        """Exercise the callback and boundary checks with each usage source."""
        callback = service_mod.dspy.settings.callbacks[0]
        fake_lm.history.append({"usage": {"prompt_tokens": 1000000, "completion_tokens": 1000000}})
        callback._check()
        ctx.check_budget()
        assert ctx.remaining_cost_usd() > 0
        usage.record([{"prompt_tokens": 1000000, "completion_tokens": 1000000}])
        assert ctx.remaining_cost_usd() == 0
        with pytest.raises(BudgetReached):
            callback._check()
        with pytest.raises(BudgetReached):
            ctx.check_budget()
        return Result(best_candidate="hello world", best_score=0.5, total_evals=0), []

    monkeypatch.setattr(service_mod, "build_scorer", lambda *args, **kwargs: ManagedScorer())
    monkeypatch.setattr(service_mod, "run_strategy", strategy)
    run_blackbox_optimization(
        _payload(
            max_cost_cents=1,
            reflection_model_config={"name": "fake/model", "token_source": "byok"},
            scorer={"kind": "python", "metric_code": VOWEL_SCORER_CODE, "model": {"name": "openai/gpt-4o-mini"}},
        ),
        artifact_id="legacy-mixed-budget",
        gepa_log_dir_path=str(tmp_path),
    )


def test_legacy_byok_reflection_counts_managed_native_model(
    fake_lm: FakeReflectionLM,
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    fake_native_proposers: list[tuple[str, EngineContext]],
) -> None:
    """Count a managed native model without counting BYOK primary usage."""

    def strategy(_strategy: Any, _task: Task, _server: EvalServer, ctx: EngineContext, *_args: Any, **_kwargs: Any):
        """Check the budget after each native model's usage arrives."""
        ctx.native_options.usage_by_model["openai/gpt-4o-mini"] = {
            "prompt_tokens": 1000000,
            "completion_tokens": 1000000,
        }
        ctx.check_budget()
        ctx.native_options.usage_by_model["openai/gpt-4o"] = {
            "prompt_tokens": 1000000,
            "completion_tokens": 1000000,
        }
        with pytest.raises(BudgetReached):
            ctx.check_budget()
        return Result(best_candidate="hello world", best_score=0.5, total_evals=0), []

    monkeypatch.setattr(service_mod, "run_strategy", strategy)
    run_blackbox_optimization(
        _payload(
            strategy={"mode": "single", "engine": "shinka_evolve"},
            max_cost_cents=1,
            reflection_model_config={"name": "openai/gpt-4o-mini", "token_source": "byok"},
            extra_reflection_model_configs=[{"name": "openai/gpt-4o", "token_source": "managed"}],
        ),
        artifact_id="legacy-mixed-native-budget",
        gepa_log_dir_path=str(tmp_path),
    )


def test_regression_guard_restores_the_seed(
    fake_lm: FakeReflectionLM, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """A winner that scores below the starting point in the final run is discarded."""
    monkeypatch.setattr(
        service_mod,
        "run_strategy",
        lambda strategy, task, server, ctx, cb=None, *, caps=None: (
            Result(best_candidate="xyz", best_score=0.0, total_evals=0),
            [],
        ),
    )

    response = run_blackbox_optimization(_payload(), artifact_id="job-3", gepa_log_dir_path=str(tmp_path))

    assert response.regression_guard_applied is True
    assert response.best_candidate == "hello world"
    assert response.best_score == response.baseline_score
    assert response.metric_improvement == 0.0
    assert response.engine_used == BLACKBOX_ENGINE_BEST_OF_N


_ILLEGAL_WINS_ON_SCORE_CODE = """
def score(candidate, case=None):
    if candidate == "xyz":
        return {"score": 0.0, "feedback": "rule broken", "illegal_approach": {"rule": "r1", "reason": "cheats"}}
    return {"score": -3.2, "feedback": "legal"}
"""


def test_regression_guard_refuses_an_illegal_winner_with_a_higher_score(
    fake_lm: FakeReflectionLM, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """A judge-rejected winner never wins, even when its fixed score beats negative legal scores."""
    monkeypatch.setattr(
        service_mod,
        "run_strategy",
        lambda strategy, task, server, ctx, cb=None, *, caps=None: (
            Result(best_candidate="xyz", best_score=0.0, total_evals=0),
            [],
        ),
    )

    response = run_blackbox_optimization(
        _payload(scorer={"kind": "python", "metric_code": _ILLEGAL_WINS_ON_SCORE_CODE}),
        artifact_id="job-3b",
        gepa_log_dir_path=str(tmp_path),
    )

    assert response.regression_guard_applied is True
    assert response.best_candidate == "hello world"
    assert response.best_score == response.baseline_score == pytest.approx(-3.2)


_LEGAL_RUNNER_UP_CODE = """
def score(candidate, case=None):
    if candidate == "xyz":
        return {"score": 0.0, "feedback": "rule broken", "illegal_approach": {"rule": "r1", "reason": "cheats"}}
    if candidate == "legal-better":
        return {"score": -2.0, "feedback": "legal improvement"}
    return {"score": -3.2, "feedback": "legal"}
"""


def test_a_rejected_winner_gives_way_to_the_best_legal_version(
    fake_lm: FakeReflectionLM, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """With negative scores, a judge-rejected winner yields to the best legal version, not the seed."""

    def search(
        strategy: object, task: object, server: EvalServer, ctx: object, cb: object = None, *, caps: object = None
    ) -> tuple[Result, list[object]]:
        """Score a legal improvement and a rejected version, then return the server's top score."""
        server.evaluate("legal-better")
        server.evaluate("xyz")
        return Result(best_candidate=server.best_candidate, best_score=server.best_score, total_evals=server.used), []

    monkeypatch.setattr(service_mod, "run_strategy", search)

    response = run_blackbox_optimization(
        _payload(scorer={"kind": "python", "metric_code": _LEGAL_RUNNER_UP_CODE}),
        artifact_id="job-3c",
        gepa_log_dir_path=str(tmp_path),
    )

    assert response.regression_guard_applied is False
    assert response.best_candidate == "legal-better"
    assert response.best_score == pytest.approx(-2.0)
    assert response.baseline_score == pytest.approx(-3.2)


_CRASHING_RUNNER_UP_CODE = """
def score(candidate, case=None):
    if candidate == "broken":
        raise RuntimeError("syntax error in the version")
    if candidate == "xyz":
        return {"score": -0.5, "feedback": "rule broken", "illegal_approach": {"rule": "r1", "reason": "cheats"}}
    if candidate == "legal-better":
        return {"score": -2.0, "feedback": "legal improvement"}
    return {"score": -3.2, "feedback": "legal"}
"""


def test_a_version_that_crashed_the_scorer_is_not_the_legal_fallback(
    fake_lm: FakeReflectionLM, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """A crashed version's 0.0 floor beats negative legal scores, yet the fallback skips it."""

    def search(
        strategy: object, task: object, server: EvalServer, ctx: object, cb: object = None, *, caps: object = None
    ) -> tuple[Result, list[object]]:
        """Score a legal improvement, a rejected version and a crashing one; return the rejected one."""
        server.evaluate("legal-better")
        server.evaluate("xyz")
        server.evaluate("broken")
        return Result(best_candidate="xyz", best_score=-0.5, total_evals=server.used), []

    monkeypatch.setattr(service_mod, "run_strategy", search)

    response = run_blackbox_optimization(
        _payload(scorer={"kind": "python", "metric_code": _CRASHING_RUNNER_UP_CODE}),
        artifact_id="job-3d",
        gepa_log_dir_path=str(tmp_path),
    )

    assert response.regression_guard_applied is False
    assert response.best_candidate == "legal-better"
    assert response.best_score == pytest.approx(-2.0)


_PARTLY_CRASHING_RUNNER_UP_CODE = """
def score(candidate, case=None):
    if candidate == "flaky":
        if case and case.get("i") == 0:
            raise IndexError("edge case")
        return {"score": -1.0, "feedback": "ok"}
    if candidate == "xyz":
        return {"score": 0.0, "feedback": "rule broken", "illegal_approach": {"rule": "r1", "reason": "cheats"}}
    if candidate == "legal-better":
        return {"score": -2.0, "feedback": "legal improvement"}
    return {"score": -3.2, "feedback": "legal"}
"""


def test_a_version_that_crashed_on_an_earlier_case_is_not_the_legal_fallback(
    fake_lm: FakeReflectionLM, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """A version that crashed on one case but not the last is still skipped as the fallback."""
    cases = [{"i": 0}, {"i": 1}]

    def search(
        strategy: object, task: object, server: EvalServer, ctx: object, cb: object = None, *, caps: object = None
    ) -> tuple[Result, list[object]]:
        """Score each version on every case, with the flaky one crashing on the first; return the rejected one."""
        for case in cases:
            server.evaluate("legal-better", case)
            server.evaluate("xyz", case)
            server.evaluate("flaky", case)
        return Result(best_candidate="xyz", best_score=0.0, total_evals=server.used), []

    monkeypatch.setattr(service_mod, "run_strategy", search)

    response = run_blackbox_optimization(
        _payload(scorer={"kind": "python", "metric_code": _PARTLY_CRASHING_RUNNER_UP_CODE}, cases=cases),
        artifact_id="job-3e",
        gepa_log_dir_path=str(tmp_path),
    )

    assert response.regression_guard_applied is False
    assert response.best_candidate == "legal-better"
    assert response.best_score == pytest.approx(-2.0)


def test_seedless_run_without_cases_scores_the_version_alone(fake_lm: FakeReflectionLM, tmp_path: Path) -> None:
    """Without a starting point or cases there is no baseline and the scorer sees ``case=None``."""
    response = run_blackbox_optimization(
        _payload(seed_candidate=None, cases=None, budget={"max_scorer_runs": 3}),
        artifact_id="job-4",
        gepa_log_dir_path=str(tmp_path),
    )

    assert response.seed_candidate is None
    assert response.baseline_score is None
    assert response.metric_improvement is None
    assert response.case_count == 0
    assert response.case_results == []
    assert response.final_scorer_runs == 1
    assert response.best_feedback == "5 vowel(s)"
    assert response.best_candidate == "aeiou"
    assert response.best_score == 1.0


def test_run_fails_when_the_scorer_rejects_the_starting_point(fake_lm: FakeReflectionLM, tmp_path: Path) -> None:
    """A scorer that raises on the seed fails the job with a clear message."""
    payload = _payload(scorer={"kind": "python", "metric_code": "def score(c, case=None): raise KeyError('nope')"})

    with pytest.raises(ServiceError, match="scorer failed on the starting point: KeyError: 'nope'"):
        run_blackbox_optimization(payload, artifact_id="job-5", gepa_log_dir_path=str(tmp_path))


def test_validate_payload_checks_engine_and_scorer_code(monkeypatch: pytest.MonkeyPatch) -> None:
    """Validation rejects unavailable engines and unloadable scorer code, and passes clean payloads."""
    checked: list[str] = []
    monkeypatch.setattr(service_mod, "validate_scorer_code", checked.append)
    monkeypatch.setattr(service_mod, "native_runtime_unavailable_reason", lambda _runtime, _settings: "missing runtime")

    validate_blackbox_payload(_payload())
    assert checked == [VOWEL_SCORER_CODE]

    with pytest.raises(ServiceError, match="not available"):
        validate_blackbox_payload(_payload(strategy={"mode": "single", "engine": "autoresearch"}))

    remote = _payload(scorer={"kind": "remote", "url": "https://scorer.example"})
    checked.clear()
    validate_blackbox_payload(remote)
    assert checked == []


def test_validate_payload_surfaces_sandbox_errors() -> None:
    """Broken scorer code is caught by the real sandbox before the job is queued."""
    with pytest.raises(ServiceError, match="must define a function named 'score"):
        validate_blackbox_payload(_payload(scorer={"kind": "python", "metric_code": "x = 1"}))


def test_protected_contract_validation_never_loads_authored_scorer(monkeypatch: pytest.MonkeyPatch) -> None:
    """Keep executable scorer verification inside the protected runtime's actual readiness check."""

    def forbid(_code: str) -> None:
        """Reject scorer execution in contract-only validation."""
        pytest.fail("The protected parent must not execute authored scorer code.")

    monkeypatch.setattr(service_mod, "validate_scorer_code", forbid)
    validate_blackbox_payload(_payload(scorer={"kind": "python", "metric_code": "x = 1"}), verify_scorer=False)


_AGENT_TARGET = {
    "kind": "agent",
    "harness": "custom",
    "model": "m",
    "run_command": "run-agent",
    "timeout_seconds": 600,
    "concurrency": 2,
}


def test_validate_payload_rejects_agent_targets_this_deployment_cannot_run(monkeypatch: pytest.MonkeyPatch) -> None:
    """An agent target on a deployment with no sandbox is refused before it is queued."""
    monkeypatch.setattr(service_mod, "current_sandbox_runtime", lambda: None)
    monkeypatch.setattr(
        service_mod, "agent_target_unavailable_reason", lambda settings: "Agent sandboxes are not configured."
    )

    with pytest.raises(ServiceError, match="Agent targets cannot run on this deployment: Agent sandboxes are not"):
        validate_blackbox_payload(_payload(target=_AGENT_TARGET))


def test_agent_target_runs_every_scorer_call_in_its_own_sandbox(
    fake_lm: FakeReflectionLM, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Each scorer run opens a fresh, tagged sandbox, runs the harness, and the scorer judges its answer."""
    runtime = FakeSandboxRuntime()
    monkeypatch.setattr(service_mod, "agent_target_unavailable_reason", lambda settings: None)
    monkeypatch.setattr(service_mod, "sandbox_runtime_from_settings", lambda settings: runtime)
    monkeypatch.setattr(
        service_mod,
        "gateway_from_settings",
        lambda settings: GatewayConfig(url="https://gw.example/v1", api_key="secret-key"),
    )

    response = run_blackbox_optimization(
        _payload(
            seed_candidate="be a helpful agent",
            cases=[{"prompt": f"solve {i}", "i": i} for i in range(10)],
            budget={"max_scorer_runs": 4},
            scorer={"kind": "python", "metric_code": AGENT_OUTPUT_SCORER_CODE},
            target=_AGENT_TARGET,
        ),
        artifact_id="agent-1",
        gepa_log_dir_path=str(tmp_path),
    )

    assert runtime.sessions, "no sandbox was opened for the agent target"
    assert len(runtime.sessions) == len(runtime.specs)
    assert all(session.closed for session in runtime.sessions)
    assert all(session.commands == ["run-agent"] for session in runtime.sessions)

    assert len({spec.name for spec in runtime.specs}) == len(runtime.specs)
    spec = runtime.specs[0]
    assert spec.name.startswith("app-agent-1-")
    assert spec.tags == {"skynet_job": "agent-1"}
    assert spec.env["APP_MODEL"] == "m"
    assert spec.env["APP_GATEWAY_URL"] == "https://gw.example/v1"
    assert spec.env["APP_API_KEY"] == "secret-key"
    assert spec.lifetime_seconds == 60 + 600 + 15

    # The default fake answers ``done`` (two vowels of four) for every run, so
    # the scorer that reads the answer file scores every version at 0.5.
    assert response.baseline_score == pytest.approx(0.5)
    assert response.best_score == pytest.approx(0.5)
    assert response.regression_guard_applied is False

    target_meta = response.optimization_metadata["target"]
    assert target_meta["kind"] == "agent"
    assert target_meta["harness"] == "custom"
    assert target_meta["model"] == "m"
    assert target_meta["concurrency"] == 2


_JUDGE_SCORER_CODE = "def score(candidate, case=None):\n    return float(llm(candidate, case['target'])), {'feedback': 'judged', 'judge': 'fake'}\n"


@pytest.mark.parametrize(
    "strategy",
    [
        {"mode": "auto"},
        {"mode": "single", "engine": "autoresearch"},
        {"mode": "single", "engine": "meta_harness"},
    ],
)
def test_unavailable_native_recipe_fails_before_building_a_scorer(
    monkeypatch: pytest.MonkeyPatch, strategy: dict[str, str]
) -> None:
    """Reject unavailable native execution before a baseline or any paid setup starts.

    Args:
        monkeypatch: Pytest fixture for deterministic runtime capabilities.
        strategy: Recipe requiring native proposals.
    """
    monkeypatch.setattr(service_mod, "native_runtime_unavailable_reason", lambda _runtime, _settings: "missing runtime")

    def unexpected_scorer(*args: Any, **kwargs: Any) -> None:
        """Fail if scorer construction starts for a rejected recipe.

        Args:
            *args: Unexpected scorer arguments.
            **kwargs: Unexpected scorer options.
        """
        pytest.fail("Scorer was constructed before native execution was validated")

    monkeypatch.setattr(service_mod, "build_scorer", unexpected_scorer)

    with pytest.raises(ServiceError, match="missing runtime"):
        run_blackbox_optimization(_payload(strategy=strategy, max_cost_cents=100), artifact_id="invalid-native")


@pytest.mark.parametrize(
    "strategy",
    [
        {"mode": "auto"},
        {"mode": "single", "engine": "gepa"},
        {"mode": "single", "engine": "best_of_n"},
        {"mode": "single", "engine": "autoresearch"},
        {"mode": "single", "engine": "meta_harness"},
        {"mode": "single", "engine": "autosaddler"},
    ],
)
@pytest.mark.parametrize("cases", [None, _CASES[:1], _CASES])
def test_every_engine_accepts_zero_one_or_many_cases(
    monkeypatch: pytest.MonkeyPatch, strategy: dict[str, str], cases: list[dict[str, Any]] | None
) -> None:
    """No engine asks for a minimum number of cases any more.

    Args:
        monkeypatch: Pytest fixture for deterministic runtime capabilities.
        strategy: The engine or recipe being validated.
        cases: No cases, one case, or many.
    """
    monkeypatch.setattr(service_mod, "native_runtime_unavailable_reason", lambda _runtime, _settings: None)
    monkeypatch.setattr(service_mod, "validate_scorer_code", lambda _code: None)

    validate_blackbox_payload(_payload(strategy=strategy, max_cost_cents=100, cases=cases))


def test_combined_usage_preserves_distinct_native_model_keys() -> None:
    """Merge shared model usage while retaining tokens from other native model identities."""
    lm = FakeReflectionLM(model="fake/model")
    lm("reflection")
    native = NativeOptions(
        runtime="vercel",
        model="fake/model",
        gateway=GatewayConfig(url="https://unused.example/v1", api_key="test-key"),
        max_token_cost=1.0,
        usage_by_model={
            "fake/model": {"prompt_tokens": 3, "completion_tokens": 1},
            "native/other-model": {"prompt_tokens": 7, "completion_tokens": 2},
        },
    )

    assert {row.model: (row.input_tokens, row.output_tokens) for row in service_mod._combined_usage([lm], native)} == {
        "fake/model": (13, 6),
        "native/other-model": (7, 2),
    }
    assert native.usage_by_model["fake/model"] == {"prompt_tokens": 3, "completion_tokens": 1}


def test_native_call_count_sums_in_sandbox_requests() -> None:
    """Proposer CLI requests made inside the sandbox count toward the run's LM calls."""
    native = NativeOptions(
        runtime="vercel",
        model="fake/model",
        gateway=GatewayConfig(url="https://unused.example/v1", api_key="test-key"),
        max_token_cost=1.0,
        usage_by_model={
            "fake/model": {"prompt_tokens": 3, "calls": 4},
            "native/other-model": {"prompt_tokens": 7, "calls": 2},
            "native/legacy": {"prompt_tokens": 1},
        },
    )

    assert service_mod._native_call_count(native) == 6
    assert service_mod._native_call_count(None) == 0


@pytest.mark.parametrize(
    ("strategy", "native"),
    [
        ({"mode": "auto"}, True),
        ({"mode": "single", "engine": "meta_harness"}, True),
        ({"mode": "single", "engine": "autoresearch"}, True),
        ({"mode": "single", "engine": "gepa"}, False),
        ({"mode": "single", "engine": "best_of_n"}, False),
    ],
)
@pytest.mark.parametrize(
    "model_options",
    [
        {"temperature": 0.2},
        {"max_tokens": 4096},
        {"base_url": "https://models.example/v1"},
        {"extra": {"top_p": 0.9}},
        {"extra": {"reasoning_effort": "high$(id)"}},
    ],
    ids=["temperature", "max-tokens", "base-url", "extra", "unknown-effort"],
)
def test_native_model_controls_are_rejected_without_restricting_direct_engines(
    monkeypatch: pytest.MonkeyPatch, strategy: dict[str, str], native: bool, model_options: dict[str, Any]
) -> None:
    """Reject native model knobs that upstream cannot honor while retaining direct engine controls.

    Args:
        monkeypatch: Pytest fixture for deterministic runtime capabilities.
        strategy: Recipe whose model controls are validated.
        native: Whether the recipe includes an upstream native proposer.
        model_options: Explicit sampler or routing options on the submitted model.
    """
    monkeypatch.setattr(service_mod, "native_runtime_unavailable_reason", lambda _runtime, _settings: None)
    payload = _payload(
        strategy=strategy,
        max_cost_cents=100,
        scorer={"kind": "remote", "url": "https://scorer.example"},
        reflection_model_config={"name": "fake/model", **model_options},
    )

    if native:
        with pytest.raises(ServiceError, match="custom sampling and routing settings are unsupported"):
            validate_blackbox_payload(payload)
    else:
        validate_blackbox_payload(payload)


def test_scorer_llm_usage_is_billed_with_the_run(
    fake_lm: FakeReflectionLM, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """A scorer's ``llm()`` reaches the chosen model through the gateway, and its tokens land in the run's usage."""
    chosen: list[str] = []
    with FakeGateway(reply="0.5", usage=(3, 1)) as judge:

        def fake_gateway(config: Any, settings: Any) -> ScorerGateway:
            """Point the scorer at the fake judge, remembering which model was asked for."""
            chosen.append(config.name)
            return ScorerGateway(url=judge.url, model="judge", api_key="k", billing_model=config.name)

        monkeypatch.setattr(scorer_mod, "scorer_gateway", fake_gateway)
        response = run_blackbox_optimization(
            _payload(scorer={"kind": "python", "metric_code": _JUDGE_SCORER_CODE, "model": {"name": "fake/judge"}}),
            artifact_id="job-judge",
            gepa_log_dir_path=str(tmp_path),
        )

    assert chosen == ["fake/judge"]
    chats = [request["body"]["messages"] for request in judge.requests]
    assert chats
    assert [{"role": "system", "content": "hello world"}, {"role": "user", "content": "aeiou"}] in chats
    assert all(chat[0]["role"] == "system" and chat[1] == {"role": "user", "content": "aeiou"} for chat in chats)
    assert all(r["authorization"] == "Bearer k" and r["body"]["model"] == "judge" for r in judge.requests)
    assert response.baseline_score == 0.5
    calls = len(judge.requests)
    usage = {u.model: (u.input_tokens, u.output_tokens) for u in response.usage_by_model}
    assert usage["fake/judge"] == (3 * calls, calls)
    assert usage["fake/model"] == (10 * len(fake_lm.history), 5 * len(fake_lm.history))
    assert response.num_lm_calls == len(fake_lm.history) + calls
    assert response.total_tokens == 15 * len(fake_lm.history) + 4 * calls


def test_gateway_prefixed_reflection_usage_merges_with_the_scorer_row(
    fake_lm: FakeReflectionLM, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """A reflection LM on the managed gateway and a scorer on the same model report one usage row."""
    fake_lm.model = "litellm_proxy/fake/judge"
    with FakeGateway(reply="0.5", usage=(3, 1)) as judge:
        monkeypatch.setattr(
            scorer_mod,
            "scorer_gateway",
            lambda config, settings: ScorerGateway(
                url=judge.url, model="judge", api_key="k", billing_model=config.name
            ),
        )
        response = run_blackbox_optimization(
            _payload(scorer={"kind": "python", "metric_code": _JUDGE_SCORER_CODE, "model": {"name": "fake/judge"}}),
            artifact_id="job-judge-merged",
            gepa_log_dir_path=str(tmp_path),
        )
    calls = len(judge.requests)
    assert response.usage_by_model == [
        ModelTokenUsage(
            model="fake/judge",
            input_tokens=10 * len(fake_lm.history) + 3 * calls,
            output_tokens=5 * len(fake_lm.history) + calls,
        )
    ]


def test_reflection_caller_hands_chat_messages_to_the_model() -> None:
    """Text prompts pass positionally, chat messages via ``messages=``, and every call is timed."""
    calls: list[tuple[Any, Any]] = []

    class LM:
        def __call__(self, prompt: Any = None, *, messages: Any = None) -> list[str]:
            """Record whether reflection uses a text prompt or chat messages.

            Args:
                prompt: Positional reflection text.
                messages: Structured reflection messages.

            Returns:
                A deterministic reflection response.
            """
            calls.append((prompt, messages))
            return ["reflected"]

    reflection_lm, durations_ms = service_mod._reflection_caller(LM())  # type: ignore[arg-type]
    assert reflection_lm("plain text") == "reflected"
    multimodal = [{"role": "user", "content": [{"type": "text", "text": "look"}]}]
    assert reflection_lm(multimodal) == "reflected"
    assert calls == [("plain text", None), (None, multimodal)]
    assert len(durations_ms) == 2
    assert all(d >= 0 for d in durations_ms)


def test_run_persists_every_version_it_scored(fake_lm: FakeReflectionLM, tmp_path: Path) -> None:
    """Record search evaluations in order and keep the baseline in its dedicated result fields.

    Args:
        fake_lm: Metered model fake used by upstream Best-of-N.
        tmp_path: Per-test artifact directory.
    """
    response = run_blackbox_optimization(_payload(), artifact_id="job-v", gepa_log_dir_path=str(tmp_path))

    assert response.versions
    assert response.seed_candidate == "hello world"
    assert response.baseline_score == pytest.approx(3 / 11)
    assert response.versions[0].candidate == "aeiou"
    assert [version.first_run for version in response.versions] == sorted(
        version.first_run for version in response.versions
    )
    assert all(version.evals >= 1 and version.score is not None for version in response.versions)
    assert response.versions[0].side_info == {"feedback": "5 vowel(s)", "vowels": 5}
    assert max(response.versions, key=lambda version: version.score or 0.0).candidate == "aeiou"


def test_run_carries_gepa_lineage_as_a_candidate_tree(fake_lm: FakeReflectionLM, tmp_path: Path) -> None:
    """A GEPA run's parent lineage arrives as a tree rooted at the seed, kept out of ``details``."""
    payload = _payload(strategy={"mode": "single", "engine": BLACKBOX_ENGINE_GEPA})
    response = run_blackbox_optimization(payload, artifact_id="job-tree", gepa_log_dir_path=str(tmp_path))

    assert response.candidate_tree
    assert response.candidate_tree[0].candidate == "hello world"
    assert response.candidate_tree[0].parents == [None]
    assert all(
        parent is None or 0 <= parent < index
        for index, node in enumerate(response.candidate_tree)
        for parent in node.parents
    )
    assert any(node.candidate == response.best_candidate for node in response.candidate_tree)
    assert "candidate_tree" not in response.details


def test_version_history_sheds_images_from_the_weakest_versions_first(monkeypatch: pytest.MonkeyPatch) -> None:
    """Over the byte cap, weak versions lose their renders but keep their text; the best keeps everything."""
    image = "data:image/png;base64," + "A" * 400

    def rendering_scorer(candidate: Any, case: Any = None) -> tuple[float, dict[str, Any]]:
        """Score by length and attach a render plus feedback.

        Args:
            candidate: The version.
            case: Ignored.

        Returns:
            The length score and side info with one image.
        """
        return float(len(candidate)), {"feedback": f"len {len(candidate)}", "render": image, "frames": [image]}

    server = service_mod.EvalServer(rendering_scorer, max_evals=10)
    for candidate in ("a", "ccc", "bb"):
        server.evaluate(candidate)
    monkeypatch.setattr(service_mod, "VERSION_SIDE_INFO_BYTE_CAP", 1_000)

    versions = service_mod._version_history(server)

    assert [version.candidate for version in versions] == ["a", "ccc", "bb"]
    weakest, best, middle = versions
    assert weakest.side_info == {"feedback": "len 1", "frames": []}
    assert middle.side_info == {"feedback": "len 2", "frames": []}
    assert best.side_info == {"feedback": "len 3", "render": image, "frames": [image]}


def test_service_hands_the_progress_sink_to_the_engine_context(
    fake_lm: FakeReflectionLM, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """The job's progress sink rides into the engine context so trajectory events stream live."""
    events: list[tuple[str, dict[str, Any]]] = []
    captured: dict[str, Any] = {}

    def fake_run_strategy(strategy: Any, task: Any, server: Any, ctx: Any, cb: Any = None, *, caps: Any = None) -> Any:
        """Capture the engine context and finish immediately."""
        captured["ctx"] = ctx
        return Result(best_candidate="hello world", best_score=1.0, total_evals=0), []

    monkeypatch.setattr(service_mod, "run_strategy", fake_run_strategy)

    def sink(event: str, metrics: dict[str, Any]) -> None:
        """Record events."""
        events.append((event, metrics))

    run_blackbox_optimization(_payload(), artifact_id="job-cb", progress_callback=sink, gepa_log_dir_path=str(tmp_path))
    captured["ctx"].progress_callback("trajectory", {"score": 0.5})

    assert events[-1] == ("trajectory", {"score": 0.5})


@pytest.mark.parametrize("concurrency", [1, 2])
def test_final_run_logs_a_debug_heartbeat_per_case(caplog: pytest.LogCaptureFixture, concurrency: int) -> None:
    """Final runs log one DEBUG line per case for the verbose log view, whichever pool serves them."""
    cases = [{"n": 1}, {"n": 2}]

    with caplog.at_level(logging.DEBUG, logger="core.service_gateway.optimization.blackbox.service"):
        run = service_mod._final_run(
            lambda candidate, case: (case["n"] / 4, {"feedback": f"case {case['n']}"}),
            "v",
            cases,
            label="optimized version",
            phase=PHASE_FINAL,
            concurrency=concurrency,
        )

    assert run.score == pytest.approx(0.375)
    assert run.cases == [(0.25, "case 1"), (0.5, "case 2")]
    assert run.feedback is None
    assert run.runs == 2
    heartbeats = sorted(r.getMessage() for r in caplog.records if r.levelno == logging.DEBUG)
    assert heartbeats == [
        "optimized version final run 1/2 score=0.250",
        "optimized version final run 2/2 score=0.500",
    ]


def test_final_run_stops_scheduling_cases_once_one_fails() -> None:
    """A failing case fails the run at once and the cases still queued are never scored."""
    started: list[int] = []
    lock = threading.Lock()

    def scorer(candidate: Candidate, case: Any) -> tuple[float, dict[str, Any]]:
        """Fail the first case quickly; every other case takes long enough for the cancel to land."""
        with lock:
            started.append(case["n"])
        if case["n"] == 1:
            time.sleep(0.01)
            raise ValueError("boom")
        time.sleep(0.2)
        return 1.0, {}

    cases = [{"n": n} for n in range(1, 7)]
    with pytest.raises(ServiceError, match="scorer failed on the optimized version: ValueError: boom"):
        service_mod._final_run(scorer, "v", cases, label="optimized version", phase=PHASE_FINAL, concurrency=2)

    assert set(started) <= {1, 2, 3}


def test_final_run_without_cases_scores_the_version_once() -> None:
    """Without cases the final run scores the version once and keeps its feedback and named scores."""
    seen: list[Any] = []

    def scorer(candidate: Candidate, case: Any) -> tuple[float, dict[str, Any]]:
        """Score once, reporting two named scores."""
        seen.append(case)
        return 0.8, {
            "feedback": "mostly right",
            "scores": {
                "accuracy": {"score": 0.9, "feedback": "one slip"},
                "tone": {"score": 0.7, "feedback": "a bit stiff"},
            },
        }

    run = service_mod._final_run(scorer, "v", [], label="starting point", phase=PHASE_BASELINE)

    assert seen == [None]
    assert (run.score, run.feedback, run.cases, run.runs) == (0.8, "mostly right", [], 1)
    assert {name: (entry.score, entry.feedback) for name, entry in run.named.items()} == {
        "accuracy": (0.9, "one slip"),
        "tone": (0.7, "a bit stiff"),
    }


def test_final_run_averages_named_scores_over_cases() -> None:
    """Each named score is the mean over the cases, and its feedback says what each case found."""

    def scorer(candidate: Candidate, case: Any) -> tuple[float, dict[str, Any]]:
        """Score each case with one named score."""
        return case["n"] / 2, {
            "feedback": "ok",
            "scores": {"accuracy": {"score": case["n"] / 2, "feedback": f"case {case['n']} acc"}},
        }

    run = service_mod._final_run(scorer, "v", [{"n": 1}, {"n": 2}], label="optimized version", phase=PHASE_FINAL)

    assert run.named["accuracy"].score == pytest.approx(0.75)
    assert run.named["accuracy"].feedback == "Case 1: case 1 acc\nCase 2: case 2 acc"


def test_a_failed_case_counts_as_zero_in_every_named_score() -> None:
    """A case whose run failed lowers each part's mean as it lowers the total, instead of vanishing."""

    def scorer(candidate: Candidate, case: Any) -> tuple[float, dict[str, Any]]:
        """Fail the first case before the metric; score the second on accuracy."""
        if case["n"] == 1:
            return 0.0, {"error": "TimeoutError: the run timed out"}
        return 0.8, {"feedback": "ok", "scores": {"accuracy": {"score": 0.8, "feedback": "close"}}}

    run = service_mod._final_run(scorer, "v", [{"n": 1}, {"n": 2}], label="optimized version", phase=PHASE_FINAL)

    assert run.score == pytest.approx(0.4)
    assert run.named["accuracy"].score == pytest.approx(0.4)


def test_final_run_keeps_each_cases_observation_for_both_versions() -> None:
    """A scorer that measures its runs gives each case result the baseline's and the winner's observation."""

    class ObservedScorer:
        """Scores every case 1.0 and reports a time that depends on the version."""

        def __call__(self, candidate: Candidate, case: Any) -> tuple[float, dict[str, Any]]:
            """Score without an observation (the optimizer's path)."""
            return 1.0, {"feedback": "ok"}

        def score_observed(self, candidate: Candidate, case: Any) -> tuple[float, dict[str, Any], dict[str, Any]]:
            """Score and report this run's time."""
            return 1.0, {"feedback": "ok"}, {"timing": {"task_ms": 100.0 if candidate == "slow" else 10.0}}

    scorer = ObservedScorer()
    cases = [{"n": 1}, {"n": 2}]
    baseline = service_mod._final_run(scorer, "slow", cases, label="starting point", phase=PHASE_BASELINE)
    best = service_mod._final_run(scorer, "fast", cases, label="optimized version", phase=PHASE_FINAL, concurrency=2)
    rows = service_mod._case_results(baseline, best)

    assert [row.baseline_observation["timing"]["task_ms"] for row in rows if row.baseline_observation] == [100.0] * 2
    assert [row.best_observation["timing"]["task_ms"] for row in rows if row.best_observation] == [10.0] * 2


def test_final_run_without_observations_leaves_them_empty() -> None:
    """A plain scorer function gives case results with no observation, as before."""
    run = service_mod._final_run(
        lambda c, case: (0.5, {"feedback": "f"}), "v", [{"n": 1}], label="x", phase=PHASE_FINAL
    )

    assert run.observations == [None]
    assert service_mod._case_results(run, run)[0].best_observation is None


def test_version_history_shows_the_validation_score_and_keeps_the_running_mean() -> None:
    """A version the engine scored on the validation set shows that score; the running mean rides along."""
    server = EvalServer(vowel_scorer, max_evals=4)
    server.evaluate("aaa")
    server.evaluate("aaa")
    server.evaluate("bcd")

    versions = service_mod._version_history(server, {"aaa": 0.75})

    assert [(v.candidate, v.score, v.mean_score, v.evals) for v in versions] == [
        ("aaa", 0.75, server.mean_score("aaa"), 2),
        ("bcd", server.mean_score("bcd"), server.mean_score("bcd"), 1),
    ]


def test_validation_scores_come_from_every_lane_with_the_later_lane_winning() -> None:
    """Lineage from every lane feeds the map; a candidate seen again takes the later lane's score."""
    explore = LaneOutcome(
        engine="gepa",
        phase="explore",
        status="completed",
        metadata={
            "candidate_tree": [
                {"candidate": "seed", "val_score": 0.2},
                {"candidate": {"system": "x"}, "val_score": 0.5},
                {"candidate": "never swept", "val_score": None},
            ]
        },
    )
    bare = LaneOutcome(engine="best_of_n", phase="explore", status="completed")
    continued = LaneOutcome(
        engine="gepa",
        phase="continue",
        status="completed",
        metadata={"candidate_tree": [{"candidate": "seed", "val_score": 0.4}]},
    )

    assert service_mod._validation_scores([explore, bare, continued]) == {
        "seed": 0.4,
        candidate_key({"system": "x"}): 0.5,
    }


def test_run_versions_show_the_tree_score_as_their_headline(fake_lm: FakeReflectionLM, tmp_path: Path) -> None:
    """Every version the candidate tree knows shows the tree's validation score, with its running mean alongside."""
    payload = _payload(strategy={"mode": "single", "engine": BLACKBOX_ENGINE_GEPA})
    response = run_blackbox_optimization(payload, artifact_id="job-headline", gepa_log_dir_path=str(tmp_path))

    by_candidate = {version.candidate: version for version in response.versions}
    swept = [node for node in response.candidate_tree if node.val_score is not None]
    assert swept
    for node in swept:
        version = by_candidate[node.candidate]
        assert version.score == node.val_score
        assert version.mean_score is not None
        assert version.evals >= 1


def test_final_run_rescores_both_versions_outside_the_budget(
    fake_lm: FakeReflectionLM, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """The winner is picked from the run's scores, then both versions are scored afresh on validation and test.

    Args:
        fake_lm: Metered model fake used by upstream Best-of-N.
        tmp_path: Per-test artifact directory.
        monkeypatch: Pytest fixture wrapping the job's scorer.
    """
    real_build = service_mod.build_scorer
    recorders: list[_RecordingScorer] = []

    def build(*args: Any, **kwargs: Any) -> _RecordingScorer:
        """Wrap the real sandboxed scorer so every call is recorded."""
        recorders.append(_RecordingScorer(real_build(*args, **kwargs)))
        return recorders[-1]

    monkeypatch.setattr(service_mod, "build_scorer", build)

    response = run_blackbox_optimization(_payload(), artifact_id="job-final", gepa_log_dir_path=str(tmp_path))

    calls = recorders[0].calls
    splits = service_mod.legacy_case_splits(list(_CASES), 1)
    held_out = len(splits.val) + len(splits.test)
    assert response.total_scorer_runs <= 12
    assert response.final_scorer_runs == 2 * held_out
    assert len(calls) == response.total_scorer_runs + response.final_scorer_runs
    test_ids = {case["i"] for case in splits.test}
    search_calls = calls[: response.total_scorer_runs]
    assert all(case is None or case["i"] not in test_ids for _, case in search_calls)
    final_calls = calls[-response.final_scorer_runs :]
    assert sorted(candidate for candidate, _ in final_calls) == ["aeiou"] * held_out + ["hello world"] * held_out
    assert response.baseline_score == pytest.approx(3 / 11)
    assert response.best_score == 1.0
    assert response.case_count == len(splits.test)
    assert [(row.baseline_score, row.best_score) for row in response.case_results] == [
        (pytest.approx(3 / 11), 1.0)
    ] * len(splits.test)
    assert response.case_results[0].best_feedback == "5 vowel(s)"


def test_one_case_run_scores_and_reports_that_case(fake_lm: FakeReflectionLM, tmp_path: Path) -> None:
    """A single case is enough: no split can leave an engine or the final run empty-handed.

    Args:
        fake_lm: Metered model fake used by upstream Best-of-N.
        tmp_path: Per-test artifact directory.
    """
    response = run_blackbox_optimization(
        _payload(cases=_CASES[:1], budget={"max_scorer_runs": 4}),
        artifact_id="job-one-case",
        gepa_log_dir_path=str(tmp_path),
    )

    assert response.case_count == 1
    assert response.final_scorer_runs == 2
    assert response.baseline_score == pytest.approx(3 / 11)
    assert response.best_score == 1.0
    assert len(response.case_results) == 1


@pytest.mark.parametrize("evaluated", [False, True])
def test_budget_stop_preserves_only_evaluated_incumbent_and_skips_final_test(fake_lm, tmp_path, monkeypatch, evaluated):
    """Keep a completed selection without starting a final paid holdout pass.

    Args:
        fake_lm: Deterministic reflection model.
        tmp_path: Private optimizer workspace.
        monkeypatch: Dependency replacement fixture.
        evaluated: Whether upstream has published a completed aggregate selection.
    """
    final_runs = []

    def final_run(*args, **kwargs):
        """Record which final runs were actually requested."""
        final_runs.append(kwargs["phase"])

    def strategy(*args, **kwargs):
        """Stop after the upstream incumbent publication boundary."""
        stop = BudgetReached()
        if evaluated:
            stop.result = Result(
                best_candidate="tested candidate",
                best_score=0.7,
                total_evals=3,
                metadata={"selection_source": "upstream_fixture"},
            )
            stop.evidence["selection_scope"] = "validation"
        raise stop

    monkeypatch.setattr(service_mod, "_final_run", final_run)
    monkeypatch.setattr(service_mod, "run_strategy", strategy)
    with pytest.raises(BudgetReached) as caught:
        run_blackbox_optimization(_payload(), artifact_id="budget-fixture", gepa_log_dir_path=str(tmp_path))
    assert final_runs == []
    if evaluated:
        response = caught.value.result
        assert response.best_candidate == "tested candidate"
        assert response.best_score is None
        assert response.details["optimizer_best_score"] == 0.7
        assert caught.value.evidence["candidate_origin"] == "optimized"
    else:
        response = caught.value.result
        assert response.best_candidate == "hello world"
        assert response.baseline_score is None
        assert response.best_score is None
        assert caught.value.evidence["candidate_origin"] == "seed"
    assert caught.value.evidence["final_evaluation_completed"] is False


def test_budget_stop_during_the_final_run_keeps_the_winner_unscored(fake_lm, tmp_path, monkeypatch):
    """A budget stop in the final run still reports the winner, without final scores.

    Args:
        fake_lm: Deterministic reflection model.
        tmp_path: Private optimizer workspace.
        monkeypatch: Dependency replacement fixture.
    """

    def final_run(*_args, **_kwargs):
        """Stop before returning any final score."""
        raise BudgetReached()

    monkeypatch.setattr(service_mod, "_final_run", final_run)
    with pytest.raises(BudgetReached) as caught:
        run_blackbox_optimization(_payload(), artifact_id="budget-fixture", gepa_log_dir_path=str(tmp_path))

    assert caught.value.result.best_candidate == "aeiou"
    assert caught.value.result.baseline_score is None
    assert caught.value.result.best_score is None


@pytest.mark.parametrize(
    "strategy",
    [{"mode": "auto"}, {"mode": "single", "engine": "meta_harness"}],
)
def test_budget_backed_native_run_leaves_its_cost_ceiling_to_the_ledger(
    monkeypatch: pytest.MonkeyPatch, strategy: dict[str, str]
) -> None:
    """Demand a cost ceiling only from a native run that no execution budget backs.

    Args:
        monkeypatch: Pytest fixture for deterministic runtime capabilities.
        strategy: Upstream recipe being validated.
    """
    monkeypatch.setattr(service_mod, "native_runtime_unavailable_reason", lambda _runtime, _settings: None)
    validate_blackbox_payload(_payload(strategy=strategy, execution_budget_id="budget-1"), verify_scorer=False)
    with pytest.raises(ServiceError, match="Set a total spending budget"):
        validate_blackbox_payload(_payload(strategy=strategy), verify_scorer=False)


def test_new_requests_naming_claude_code_are_rejected() -> None:
    """Refuse Claude Code as a proposer or an agent target: Skynet no longer runs it."""
    with pytest.raises(ValueError, match="Claude Code is no longer available"):
        _payload(proposer={"harness": "claude_code"})
    with pytest.raises(ValueError, match="Claude Code is no longer available"):
        _payload(target={**_AGENT_TARGET, "harness": "claude_code"})


def test_proposer_accepts_any_offered_harness_and_rejects_unlaunchable_ones() -> None:
    """Let the request pick the proposer harness and engine knobs while refusing shapes the sandbox cannot run."""
    request = _payload(proposer={"harness": "pi", "max_candidates_per_iter": 2, "ralph": False})
    assert request.proposer.harness == "pi"
    assert request.proposer.max_candidates_per_iter == 2
    assert request.proposer.ralph is False
    assert _payload().proposer.harness == "codex"
    custom = _payload(proposer={"harness": "custom", "run_command": "my-agent {prompt_file}"})
    assert custom.proposer.run_command == "my-agent {prompt_file}"
    with pytest.raises(ValueError, match="harness"):
        _payload(proposer={"harness": "aider"})
    with pytest.raises(ValueError, match="run_command"):
        _payload(proposer={"harness": "custom"})


def test_shinka_duplicate_rejection_names_its_embeddings_route() -> None:
    """Pass the scoped embeddings route only when duplicate rejection is on."""
    gateway = GatewayConfig(url="https://gw.example/v1", api_key="run-key")
    single = {"mode": "single", "engine": "shinka_evolve"}
    route = {
        "url": "https://relay/v1",
        "token": "chat",
        "model": "m",
        "embedding": {"url": "https://relay/v1", "token": "emb", "model": "openai/e"},
    }
    scoped = _payload(
        strategy=single,
        shinka={"novelty": True},
        reflection_model_config={"name": "fake/model", "extra": {"_skynet_budget_route": route}},
    )

    assert service_mod._shinka_embedding(_payload(strategy=single), gateway) is None
    assert service_mod._shinka_embedding(scoped, gateway) == {
        "model": "openai/e",
        "url": "https://relay/v1",
        "token": "emb",
    }
    unscoped = service_mod._shinka_embedding(_payload(strategy=single, shinka={"novelty": True}), gateway)
    assert unscoped == {"model": "openai/text-embedding-3-small", "url": "https://gw.example/v1", "token": "run-key"}


def test_engine_catalog_for_a_repository_offers_every_engine_and_auto() -> None:
    """A repository run can pick Auto or any single engine the deployment offers, ShinkaEvolve included."""
    catalog = service_mod.engine_catalog("repo")

    available = {entry.id for entry in catalog.engines if entry.available}
    text_available = {entry.id for entry in service_mod.engine_catalog("text").engines if entry.available}
    assert available == text_available
    shinka = next(entry for entry in catalog.engines if entry.id == "shinka_evolve")
    assert shinka.available is (shinka.id in text_available)
    assert "gepa" in available
    text_catalog = service_mod.engine_catalog("text")
    assert catalog.auto_available is text_catalog.auto_available
    assert catalog.auto_unavailable_reason == text_catalog.auto_unavailable_reason
    assert all(entry.unavailable_reason for entry in catalog.engines if not entry.available)


GEOCODE_SLOW_RUN = (
    "import time\n"
    "import dspy\n"
    "from skynet import llm\n"
    "program = dspy.Predict('message -> coordinates')\n"
    "def run(artifact, input):\n"
    "    instructions = program.signature.instructions\n"
    "    if 'slowly' in instructions:\n"
    "        time.sleep(0.15)\n"
    "    return llm(instructions, input['message'])\n"
)


def test_geocoding_final_runs_keep_raw_observations_for_both_versions() -> None:
    """Baseline and winner run the same cases under one gateway; each case keeps raw time, cost and components."""
    payload = UnifiedRunRequest.model_validate(
        {
            "task": {"objective": "geocode", "artifact": {"kind": "dspy_program", "starting_version": "Think slowly."}},
            "data": {
                "mode": "pairs",
                "cases": [
                    {"input": {"message": "Tel Aviv"}, "expected": {"coordinates": "32.0853,34.7818"}},
                    {"input": {"message": "Springfield"}, "expected": {"coordinates": "42.1,-72.59|32.0853,34.7818"}},
                ],
                "input_fields": ["message"],
            },
            "optimizer": {"choice": "dspy_gepa"},
            "models": {"reflection_model": {"name": "openrouter/task"}},
            "workspace": {"run_code": GEOCODE_SLOW_RUN, "metric_code": GEOCODE_METRIC, "metric_timeout_seconds": 30},
        }
    )
    cases = [UnifiedCase.model_validate(case) for case in payload.data.cases]
    with FakeGateway("32.0853,34.7818", cost=0.0004) as gateway:
        scorer = build_workspace_scorer(
            payload, GatewayConfig(url=gateway.url, api_key="k"), protected=False, runtime=LocalSubprocessRuntime()
        )
        try:
            baseline = service_mod._final_run(
                scorer, "Think slowly.", cases, label="starting point", phase=PHASE_BASELINE
            )
            best = service_mod._final_run(scorer, "Answer.", cases, label="optimized version", phase=PHASE_FINAL)
        finally:
            scorer.close()
    rows = service_mod._case_results(baseline, best)

    assert len(rows) == 2
    for row in rows:
        assert row.baseline_observation["timing"]["task_ms"] >= 150
        assert row.best_observation["timing"]["task_ms"] < row.baseline_observation["timing"]["task_ms"]
        for observed in (row.baseline_observation, row.best_observation):
            assert observed["cost"]["usd"] == pytest.approx(0.0004)
            assert observed["conditions"] == rows[0].baseline_observation["conditions"]
    assert set(best.named) == {"accuracy", "speed", "cost"}
    assert best.named["accuracy"].score == pytest.approx(1.0)
    assert best.named["speed"].score > baseline.named["speed"].score
    assert best.score > baseline.score


_DEEP_NEGATIVE_CODE = """
def score(candidate, case=None):
    if candidate == "broken":
        raise RuntimeError("syntax error in the version")
    if candidate == "legal-better":
        return {"score": -2e9, "feedback": "legal improvement"}
    return {"score": -3e9, "feedback": "legal"}
"""


def test_a_crashed_version_is_never_chosen_even_when_every_valid_score_is_below_the_floor(
    fake_lm: FakeReflectionLM, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Valid scores below the search floor never let a metric_error version win, show a number or log one."""

    def search(
        strategy: object, task: object, server: EvalServer, ctx: Any, cb: object = None, *, caps: object = None
    ) -> tuple[Result, list[LaneOutcome]]:
        """Score a valid version and a crashing one; return the crashed one, as an engine fooled by the floor would."""
        server.evaluate("legal-better")
        _, floor_side_info = server.evaluate("broken")
        assert floor_side_info["status"] == METRIC_ERROR
        ctx.progress_callback(PROGRESS_OPTIMIZER, {"score": SEARCH_FLOOR, "candidate": "broken"})
        lane = LaneOutcome(
            engine=BLACKBOX_ENGINE_BEST_OF_N,
            phase="explore",
            status="completed",
            best_score=SEARCH_FLOOR,
            best_candidate="broken",
        )
        result = Result(
            best_candidate="broken",
            best_score=SEARCH_FLOOR,
            total_evals=server.used,
            metadata={"candidate_tree": [{"candidate": "broken", "parents": [], "val_score": SEARCH_FLOOR}]},
        )
        return result, [lane]

    monkeypatch.setattr(service_mod, "run_strategy", search)
    events: list[tuple[str, dict[str, Any]]] = []

    response = run_blackbox_optimization(
        _payload(scorer={"kind": "python", "metric_code": _DEEP_NEGATIVE_CODE}),
        artifact_id="job-floor",
        progress_callback=lambda event, metrics: events.append((event, metrics)),
        gepa_log_dir_path=str(tmp_path),
    )

    assert response.best_candidate == "legal-better"
    assert response.best_score == pytest.approx(-2e9)
    broken = next(version for version in response.versions if version.candidate == "broken")
    assert broken.score is None
    assert broken.mean_score is None
    assert broken.side_info["status"] == METRIC_ERROR
    assert "RuntimeError: syntax error in the version" in broken.side_info["error"]
    assert response.lanes[0].best_score is None
    assert response.candidate_tree[0].val_score is None
    assert response.details["optimizer_best_score"] is None
    assert (PROGRESS_OPTIMIZER, {"score": None, "candidate": "broken"}) in events
    assert SEARCH_FLOOR not in [value for _, metrics in events for value in metrics.values()]


def test_a_metric_error_in_the_final_run_has_no_score_and_keeps_the_seed(
    fake_lm: FakeReflectionLM, tmp_path: Path, monkeypatch: pytest.MonkeyPatch, caplog: pytest.LogCaptureFixture
) -> None:
    """A winner the metric crashes on in the final run gets no score, never 0, and the seed stays."""
    real_build_scorer = service_mod.build_scorer

    def build_scorer(*args: Any, **kwargs: Any) -> Any:
        """Wrap the real scorer so the metric crashes on the winner."""
        inner = real_build_scorer(*args, **kwargs)

        class Crashing:
            """Report a metric error on the winner, like the workspace scorer does."""

            usage = inner.usage

            def __call__(self, candidate: Candidate, case: Any = None) -> tuple[float | None, dict[str, Any]]:
                """Return a metric error for the winner, else the real score.

                Args:
                    candidate: The version to score.
                    case: The case, if any.

                Returns:
                    The score and side info.
                """
                if candidate == "winner":
                    return None, {"error": "KeyError: 'answer'", "status": METRIC_ERROR}
                return inner(candidate, case)

            def close(self) -> None:
                """Close the wrapped scorer."""
                inner.close()

        return Crashing()

    monkeypatch.setattr(service_mod, "build_scorer", build_scorer)
    monkeypatch.setattr(
        service_mod,
        "run_strategy",
        lambda strategy, task, server, ctx, cb=None, *, caps=None: (
            Result(best_candidate="winner", best_score=1.0, total_evals=0),
            [],
        ),
    )

    with caplog.at_level(logging.WARNING, logger="core.service_gateway.optimization.blackbox.service"):
        response = run_blackbox_optimization(_payload(), artifact_id="job-final-crash", gepa_log_dir_path=str(tmp_path))

    assert any(
        "optimized version has no score: metric_error" in record.getMessage()
        and "KeyError: 'answer'" in record.getMessage()
        for record in caplog.records
    )
    assert response.regression_guard_applied is True
    assert response.best_candidate == "hello world"
    assert response.best_score == response.baseline_score
    assert response.best_score is not None


def test_final_run_has_no_score_when_the_metric_crashes_on_a_case(caplog: pytest.LogCaptureFixture) -> None:
    """One metric_error case nulls the version's final score and named scores, and the log shows the error."""

    def scorer(candidate: Candidate, case: Any) -> tuple[float | None, dict[str, Any]]:
        """Crash the metric on the first case; score the second."""
        if case["n"] == 1:
            return None, {"error": "ValueError: bad input", "status": METRIC_ERROR}
        return 0.8, {"feedback": "ok", "scores": {"accuracy": {"score": 0.8, "feedback": "close"}}}

    with caplog.at_level(logging.DEBUG, logger="core.service_gateway.optimization.blackbox.service"):
        run = service_mod._final_run(scorer, "v", [{"n": 1}, {"n": 2}], label="optimized version", phase=PHASE_FINAL)

    assert run.score is None
    assert run.named == {}
    assert run.cases == [(None, "ValueError: bad input"), (0.8, "ok")]
    messages = [record.getMessage() for record in caplog.records]
    assert "optimized version final run 1/2 metric_error: ValueError: bad input" in messages
    assert any("optimized version has no score: metric_error" in message for message in messages)
