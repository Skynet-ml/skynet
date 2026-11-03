"""Tests for the unified run's worker entry point: split, dispatch, selection and reporting."""

from __future__ import annotations

from typing import Any

import pytest

from core.models.unified import UnifiedCase, UnifiedRunRequest
from core.service_gateway.optimization.blackbox import service as blackbox_service
from core.service_gateway.optimization.blackbox.harness import GatewayConfig
from core.service_gateway.optimization.blackbox.protocol import Result
from core.service_gateway.optimization.blackbox.tests.mocks import FakeReflectionLM
from core.service_gateway.optimization.unified import service as unified_service
from core.service_gateway.optimization.unified.split import split_cases, split_seed

SEED = "Answer."
OPTIMIZED = "Answer carefully."


class _TableScorer:
    """Score from a ``{candidate: {case index: score}}`` table and record every call."""

    def __init__(self, table: dict[str, dict[int, float]]) -> None:
        """Keep the score table.

        Args:
            table: Per-candidate, per-case scores; missing cells score 0.5.
        """
        self.table = table
        self.calls: list[tuple[Any, int]] = []
        self.closed = False

    def __call__(self, candidate: Any, case: Any = None) -> tuple[float, dict[str, Any]]:
        """Score one candidate on one case.

        Args:
            candidate: The version.
            case: A unified case.

        Returns:
            The table's score and a feedback side info.
        """
        index = int(case.input["i"])
        self.calls.append((candidate, index))
        return self.table.get(str(candidate), {}).get(index, 0.5), {"feedback": "ok"}

    def close(self) -> None:
        """Mark the scorer closed."""
        self.closed = True


def _payload(n: int = 20, *, choice: str = "gepa", seed: int | None = 5) -> UnifiedRunRequest:
    """Build a pairs-mode prompt run.

    Args:
        n: How many cases.
        choice: The optimizer choice.
        seed: The run seed.

    Returns:
        The validated request.
    """
    return UnifiedRunRequest.model_validate(
        {
            "task": {"objective": "answer well", "artifact": {"kind": "prompt", "starting_version": SEED}},
            "data": {"mode": "pairs", "cases": [{"input": {"i": i}, "expected": "x"} for i in range(n)]},
            "optimizer": {"choice": choice, "budget": {"max_scorer_runs": 40}},
            "models": {"reflection_model": {"name": "fake/model"}},
            "workspace": {
                "run_code": "def run(artifact, input):\n    return ''",
                "metric_code": "def score(input, output):\n    return 1.0, {'feedback': ''}",
            },
            "seed": seed,
        }
    )


@pytest.fixture
def harness(monkeypatch: pytest.MonkeyPatch) -> dict[str, Any]:
    """Stub the gateway, reflection model and strategy; record what the engine sees.

    Args:
        monkeypatch: Pytest fixture.

    Returns:
        A dict the stub strategy fills with the task it got and the scorer slot.
    """
    seen: dict[str, Any] = {}
    monkeypatch.setattr(unified_service, "_gateway", lambda payload: GatewayConfig(url="http://gw", api_key="k"))
    monkeypatch.setattr(blackbox_service, "build_language_model", lambda config, **_: FakeReflectionLM())

    def strategy(strategy: Any, task: Any, server: Any, ctx: Any, progress: Any, *, caps: Any) -> Any:
        """Score both versions on every train case, then return the optimized one."""
        seen["strategy"] = strategy
        seen["task"] = task
        for case in task.cases:
            server.evaluate(task.seed_candidate, case)
            server.evaluate(OPTIMIZED, case)
        return Result(best_candidate=OPTIMIZED, best_score=1.0, total_evals=server.used), []

    monkeypatch.setattr(blackbox_service, "run_strategy", strategy)
    return seen


def _install_scorer(monkeypatch: pytest.MonkeyPatch, scorer: _TableScorer) -> None:
    """Make the unified service build ``scorer``.

    Args:
        monkeypatch: Pytest fixture.
        scorer: The scorer to hand out.
    """
    monkeypatch.setattr(unified_service, "build_workspace_scorer", lambda payload, gateway, **_: scorer)


def _splits(payload: UnifiedRunRequest) -> Any:
    """Recompute the split the service makes.

    Args:
        payload: The unified run.

    Returns:
        The run's case splits.
    """
    data = payload.data
    return split_cases(list(data.cases or []), data.mode, data.split, shuffle=data.shuffle, seed=split_seed(payload))


def _ids(cases: list[UnifiedCase]) -> set[int]:
    """Read case indices.

    Args:
        cases: Unified cases.

    Returns:
        Their ``input["i"]`` values.
    """
    return {int(case.input["i"]) for case in cases}


def test_engine_gets_train_and_val_and_never_a_test_case(
    harness: dict[str, Any], monkeypatch: pytest.MonkeyPatch, tmp_path: Any
) -> None:
    """The engine's task holds train and val only; the search scores no test case.

    Args:
        harness: Stubbed run plumbing.
        monkeypatch: Pytest fixture.
        tmp_path: Run directory.
    """
    payload = _payload()
    splits = _splits(payload)
    scorer = _TableScorer({OPTIMIZED: dict.fromkeys(range(20), 1.0)})
    _install_scorer(monkeypatch, scorer)

    response = unified_service.run_unified_optimization(payload, artifact_id="u1", gepa_log_dir_path=str(tmp_path))

    task = harness["task"]
    assert _ids(task.cases) == _ids(splits.train)
    assert _ids(task.valset) == _ids(splits.val)
    assert not (_ids(task.cases) | _ids(task.valset)) & _ids(splits.test)
    assert task.context["metric_code"] == payload.workspace.metric_code
    search_calls = scorer.calls[: response.total_scorer_runs]
    assert not {index for _, index in search_calls} & _ids(splits.test)
    assert scorer.closed is True
    assert response.split_counts == splits.counts
    assert response.optimizer_resolved == "gepa"
    assert harness["strategy"].mode == "single"
    assert harness["strategy"].engine == "gepa"


def test_final_scores_report_on_test_and_select_on_val(
    harness: dict[str, Any], monkeypatch: pytest.MonkeyPatch, tmp_path: Any
) -> None:
    """Validation scores decide the winner; the reported scores come from test only.

    Args:
        harness: Stubbed run plumbing.
        monkeypatch: Pytest fixture.
        tmp_path: Run directory.
    """
    payload = _payload()
    splits = _splits(payload)
    val_ids, test_ids = _ids(splits.val), _ids(splits.test)
    table = {
        SEED: {**dict.fromkeys(val_ids, 0.2), **dict.fromkeys(test_ids, 0.4)},
        OPTIMIZED: {**dict.fromkeys(val_ids, 0.9), **dict.fromkeys(test_ids, 0.6)},
    }
    scorer = _TableScorer(table)
    _install_scorer(monkeypatch, scorer)

    response = unified_service.run_unified_optimization(payload, artifact_id="u2", gepa_log_dir_path=str(tmp_path))

    assert response.best_candidate == OPTIMIZED
    assert response.regression_guard_applied is False
    assert response.baseline_val_score == pytest.approx(0.2)
    assert response.best_val_score == pytest.approx(0.9)
    assert response.baseline_score == pytest.approx(0.4)
    assert response.best_score == pytest.approx(0.6)
    assert response.case_count == len(test_ids)
    final_calls = scorer.calls[response.total_scorer_runs :]
    assert {index for _, index in final_calls} == val_ids | test_ids
    assert response.final_scorer_runs == 2 * (len(val_ids) + len(test_ids))


def test_regression_guard_ignores_a_test_set_win(
    harness: dict[str, Any], monkeypatch: pytest.MonkeyPatch, tmp_path: Any
) -> None:
    """A winner that loses on validation is reverted even though it wins on test.

    Args:
        harness: Stubbed run plumbing.
        monkeypatch: Pytest fixture.
        tmp_path: Run directory.
    """
    payload = _payload()
    splits = _splits(payload)
    val_ids, test_ids = _ids(splits.val), _ids(splits.test)
    table = {
        SEED: {**dict.fromkeys(val_ids, 0.8), **dict.fromkeys(test_ids, 0.1)},
        OPTIMIZED: {**dict.fromkeys(val_ids, 0.3), **dict.fromkeys(test_ids, 1.0)},
    }
    _install_scorer(monkeypatch, _TableScorer(table))

    response = unified_service.run_unified_optimization(payload, artifact_id="u3", gepa_log_dir_path=str(tmp_path))

    assert response.regression_guard_applied is True
    assert response.best_candidate == SEED
    assert response.best_score == pytest.approx(0.1)
    assert response.baseline_score == pytest.approx(0.1)


def test_few_pairs_report_on_validation(
    harness: dict[str, Any], monkeypatch: pytest.MonkeyPatch, tmp_path: Any
) -> None:
    """Below the held-out minimum the reported split is validation and nothing is held out.

    Args:
        harness: Stubbed run plumbing.
        monkeypatch: Pytest fixture.
        tmp_path: Run directory.
    """
    payload = _payload(4)
    _install_scorer(monkeypatch, _TableScorer({OPTIMIZED: dict.fromkeys(range(4), 1.0)}))

    response = unified_service.run_unified_optimization(payload, artifact_id="u4", gepa_log_dir_path=str(tmp_path))

    assert response.split_counts is not None
    assert response.split_counts.test == 0
    assert response.split_counts.held_out is False
    assert response.split_counts.report_split in {"val", "all"}
    assert response.best_score == response.best_val_score


@pytest.mark.parametrize(
    ("resolved", "mode", "engine"),
    [("auto", "auto", None), ("gepa", "single", "gepa"), ("dspy_gepa", "single", "dspy_gepa")],
)
def test_strategy_dispatch(resolved: str, mode: str, engine: str | None) -> None:
    """Auto runs the Auto strategy; every other optimizer runs as one engine.

    Args:
        resolved: The resolved optimizer.
        mode: The expected strategy mode.
        engine: The expected engine id.
    """
    strategy = unified_service.unified_strategy(resolved)
    assert (strategy.mode, strategy.engine) == (mode, engine)


def test_job_spec_tracks_sources_for_each_extra_lm() -> None:
    """Keep the task and metric source aligned with the supplied LMs."""
    payload = _payload()
    payload.models.reflection_model.token_source = "byok"
    payload.models.metric_model = payload.models.reflection_model.model_copy(update={"token_source": "managed"})
    job = unified_service.unified_job_spec(payload, seed=1, task_lm=object(), metric_lm=object())
    assert job.extra_lm_sources == ["byok", "managed"]
    metric_only = unified_service.unified_job_spec(payload, seed=1, task_lm=None, metric_lm=object())
    assert metric_only.extra_lm_sources == ["managed"]


def test_job_spec_keeps_metric_source_in_private_context() -> None:
    """The metric source rides only in the job's private context, never in the objective or seed."""
    payload = _payload()
    job = unified_service.unified_job_spec(payload, seed=1, task_lm=None)
    assert job.context["metric_code"] == payload.workspace.metric_code
    assert payload.workspace.metric_code not in (job.objective or "")
    assert job.seed_candidate == SEED
    assert job.optimizer_resolved == "gepa"


def test_worker_kwargs_reach_run_job(monkeypatch: pytest.MonkeyPatch) -> None:
    """The subprocess runner's sink, route and repo snapshot pass through to ``run_job`` unchanged."""
    _install_scorer(monkeypatch, _TableScorer({}))
    monkeypatch.setattr(unified_service, "_gateway", lambda payload: GatewayConfig(url="http://gw", api_key="k"))
    seen: dict[str, Any] = {}

    def fake_run_job(job: Any, scorer: Any, splits: Any, **kwargs: Any) -> str:
        """Record the keyword arguments ``run_job`` receives.

        Args:
            job: The job spec.
            scorer: The workspace scorer.
            splits: The case splits.
            **kwargs: Everything else.

        Returns:
            A sentinel result.
        """
        seen.update(kwargs)
        return "result"

    monkeypatch.setattr(unified_service, "run_job", fake_run_job)
    sink = object()
    route = {"url": "http://route", "token": "t"}
    snapshot = {"files": []}

    result = unified_service.run_unified_optimization(
        _payload(),
        artifact_id="job-1",
        gepa_log_dir_path="/tmp/u",
        agent_run_sink=sink,  # type: ignore[arg-type]
        target_route=route,
        evaluator_route={"url": "http://eval", "token": "e"},
        repo_snapshot=snapshot,
    )

    assert result == "result"
    assert seen["agent_run_sink"] is sink
    assert seen["target_route"] is route
    assert seen["repo_snapshot"] is snapshot
    assert seen["gepa_log_dir_path"] == "/tmp/u"
