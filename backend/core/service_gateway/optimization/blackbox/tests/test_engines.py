"""Verify upstream engine fidelity, accounting transport and registry availability."""

from __future__ import annotations

import itertools
import json
import math
import pickle
from pathlib import Path
from types import SimpleNamespace
from typing import Any
from unittest.mock import MagicMock

import dspy
import pytest
from dspy.utils.callback import BaseCallback
from gepa.oa.budget import BudgetTracker
from gepa.oa.config import OptimizeAnythingConfig
from gepa.oa.engines.best_of_n import BestOfNEngine as UpstreamBestOfN
from gepa.oa.eval_server import EvalServer as UpstreamEvalServer
from gepa.oa.task import Task as UpstreamTask

from core.constants import PROGRESS_CANDIDATE, PROGRESS_MINIBATCH
from core.exceptions import ServiceError
from core.models.blackbox import (
    BLACKBOX_ENGINE_AUTORESEARCH,
    BLACKBOX_ENGINE_AUTOSADDLER,
    BLACKBOX_ENGINE_BEST_OF_N,
    BLACKBOX_ENGINE_GEPA,
    BLACKBOX_ENGINE_META_HARNESS,
    BLACKBOX_ENGINE_SHINKA_EVOLVE,
    BlackboxStrategy,
)
from core.service_gateway.optimization.cost_ceiling import CostCeilingExceededError

from .. import auto
from .. import gepa_engine as gepa_mod
from ..autoresearch import AutoResearchEngine
from ..best_of_n import BestOfNEngine
from ..gepa_engine import GepaEngine
from ..meta_harness import MetaHarnessEngine
from ..protocol import UNSCORED, BudgetExhaustedError, EngineContext, EvalServer, Result, Task
from ..registry import ENGINES, EngineCapabilities, available_engine_ids, get_engine
from ..upstream import AUTO_ENGINES, GEPA_SOURCE
from .mocks import FakeGateway, make_ctx, vowel_scorer

_NATIVE_CAPS = EngineCapabilities(proposer_available=True)


class _SequenceModel:
    """Return deterministic completions while recording unmodified chat messages."""

    def __init__(self, completions: list[str]) -> None:
        """Store the completion sequence.

        Args:
            completions: Responses consumed in order by the real upstream engine.
        """
        self._responses = iter(completions)
        self.messages: list[list[dict[str, Any]]] = []

    def __call__(self, prompt: str | list[dict[str, Any]]) -> str:
        """Record the input and return the next completion.

        Args:
            prompt: Original upstream prompt, or its chat transport form.

        Returns:
            The next scripted completion.
        """
        messages = [{"role": "user", "content": prompt}] if isinstance(prompt, str) else prompt
        self.messages.append(messages)
        return next(self._responses)


def test_registry_requires_native_proposer_availability() -> None:
    """Keep native algorithms visible in the catalog but reject unavailable runtimes."""
    assert available_engine_ids() == [BLACKBOX_ENGINE_GEPA, BLACKBOX_ENGINE_BEST_OF_N]
    assert isinstance(get_engine(BLACKBOX_ENGINE_GEPA), GepaEngine)
    assert isinstance(get_engine(BLACKBOX_ENGINE_BEST_OF_N), BestOfNEngine)
    for engine_id in (
        BLACKBOX_ENGINE_AUTORESEARCH,
        BLACKBOX_ENGINE_META_HARNESS,
        BLACKBOX_ENGINE_AUTOSADDLER,
        BLACKBOX_ENGINE_SHINKA_EVOLVE,
    ):
        assert ENGINES[engine_id].factory is not None
        with pytest.raises(ServiceError, match="proposer runtime is not configured"):
            get_engine(engine_id)
        unavailable = EngineCapabilities(proposer_reason="Install the pinned Claude CLI")
        assert ENGINES[engine_id].unavailable_reason_for(unavailable) == "Install the pinned Claude CLI"


@pytest.mark.parametrize("sandbox", [False, True])
def test_registry_native_algorithms_do_not_depend_on_candidate_target(sandbox: bool) -> None:
    """Allow native proposal execution for scored code once its runtime is ready."""
    caps = EngineCapabilities(sandbox=sandbox, agent_target=False, proposer_available=True)
    assert available_engine_ids(caps) == [
        BLACKBOX_ENGINE_GEPA,
        BLACKBOX_ENGINE_BEST_OF_N,
        BLACKBOX_ENGINE_AUTORESEARCH,
        BLACKBOX_ENGINE_META_HARNESS,
        BLACKBOX_ENGINE_AUTOSADDLER,
        BLACKBOX_ENGINE_SHINKA_EVOLVE,
    ]
    assert isinstance(get_engine(BLACKBOX_ENGINE_META_HARNESS, caps), MetaHarnessEngine)
    assert isinstance(get_engine(BLACKBOX_ENGINE_AUTORESEARCH, caps), AutoResearchEngine)


def test_registry_filters_multi_part_engines() -> None:
    """Expose named-component inputs only for engines whose pinned adapter supports them."""
    assert available_engine_ids(parts=True) == [BLACKBOX_ENGINE_GEPA]
    assert available_engine_ids(_NATIVE_CAPS, parts=True) == [
        BLACKBOX_ENGINE_GEPA,
        BLACKBOX_ENGINE_AUTOSADDLER,
        BLACKBOX_ENGINE_SHINKA_EVOLVE,
    ]


def test_registry_rejects_unknown_engine() -> None:
    """Name only algorithms runnable with the supplied execution capabilities."""
    with pytest.raises(ServiceError, match=r"Unknown engine 'nope'\. Available engines: gepa, best_of_n\."):
        get_engine("nope")
    with pytest.raises(
        ServiceError,
        match=r"Available engines: gepa, best_of_n, autoresearch, meta_harness, autosaddler, shinka_evolve\.",
    ):
        get_engine("nope", _NATIVE_CAPS)


def test_best_of_n_matches_direct_upstream_sampling(tmp_path: Path) -> None:
    """Preserve upstream prompts, case selection, independent samples and aggregate winner."""
    completions = ["```text\nhello\n```", "```\naeiou\n```", "xyz"]
    direct_model = _SequenceModel(completions)
    adapter_model = _SequenceModel(completions)
    direct_calls: list[tuple[Any, Any]] = []
    adapter_calls: list[tuple[Any, Any]] = []
    task = Task(
        seed_candidate="never score this seed",
        objective="more vowels",
        background="Keep the task unchanged",
        cases=[{"id": "train-a"}, {"id": "train-b"}],
    )

    def direct_score(candidate: Any, example: Any = None) -> tuple[float, dict[str, Any]]:
        """Record direct-upstream evaluations before applying the deterministic metric.

        Args:
            candidate: Proposed text.
            example: Upstream-selected task example.

        Returns:
            Vowel density and feedback.
        """
        direct_calls.append((candidate, example))
        return vowel_scorer(candidate, example)

    def adapter_score(candidate: Any, example: Any = None) -> tuple[float, dict[str, Any]]:
        """Record adapted evaluations before applying the same metric.

        Args:
            candidate: Proposed text.
            example: Upstream-selected task example.

        Returns:
            Vowel density and feedback.
        """
        adapter_calls.append((candidate, example))
        return vowel_scorer(candidate, example)

    upstream_task = UpstreamTask(
        name="direct",
        seed_candidate=task.seed_candidate,
        objective=task.objective,
        background=task.background,
        train_set=task.cases,
    )
    upstream = UpstreamEvalServer(upstream_task, direct_score, BudgetTracker(max_evals=6))
    with FakeGateway(reply=lambda body: direct_model(body["messages"])) as gateway:
        expected = UpstreamBestOfN(
            OptimizeAnythingConfig(
                engine="best_of_n",
                max_evals=6,
                engine_config={
                    "model": "openai/direct-fixture",
                    "lm_kwargs": {"api_base": gateway.url, "api_key": "fixture", "num_retries": 0},
                },
            )
        ).run(upstream_task, upstream)
    server = EvalServer(adapter_score, max_evals=6)
    actual = BestOfNEngine().run(task, server, EngineContext(reflection_lm=adapter_model, run_dir=str(tmp_path)))

    assert (actual.best_candidate, actual.best_score) == (expected.best_candidate, expected.best_score)
    assert actual.best_candidate == "aeiou"
    assert actual.total_evals == server.used == upstream.budget.used == 6
    assert adapter_calls == direct_calls
    assert all(candidate != task.seed_candidate for candidate, _ in adapter_calls)
    assert adapter_model.messages == direct_model.messages
    assert adapter_model.messages == [adapter_model.messages[0]] * 3
    assert actual.metadata["n_samples"] == expected.metadata["n_samples"] == 3
    assert actual.metadata["upstream_source"] == GEPA_SOURCE
    persisted = [json.loads(line) for line in (tmp_path / "bon_cost_log.jsonl").read_text().splitlines()]
    assert [entry["score"] for entry in persisted] == [entry["score"] for entry in expected.eval_log]


def test_best_of_n_keeps_completed_incumbent_when_next_candidate_is_partial(tmp_path: Path) -> None:
    """Exclude a promising one-case result when the budget cuts its evaluation short."""
    model = _SequenceModel(["completed", "incomplete"])

    def score(candidate: Any, example: Any = None) -> tuple[float, dict[str, Any]]:
        """Give the incomplete proposal a tempting score on its first case.

        Args:
            candidate: Scripted proposal.
            example: Dataset example.

        Returns:
            Deterministic case score and no feedback.
        """
        return (0.4 if candidate == "completed" else 1.0), {}

    server = EvalServer(score, max_evals=3)
    task = Task(seed_candidate="seed", cases=[{"id": "a"}, {"id": "b"}])
    result = BestOfNEngine().run(task, server, EngineContext(reflection_lm=model, run_dir=str(tmp_path)))

    assert server.best_candidate == "incomplete"
    assert result.best_candidate == "completed"
    assert result.best_score == 0.4
    assert result.total_evals == 3
    assert len(result.metadata["bon_cost_log"]) == 1


def test_best_of_n_streams_each_completed_sample_as_a_root(tmp_path: Path) -> None:
    """Announce every fully scored sample, with its case scores, and never a partial one."""
    model = _SequenceModel(["aeiou", "xyz", "cut short"])
    sink: list[tuple[str, dict[str, Any]]] = []
    server = EvalServer(vowel_scorer, max_evals=5)
    task = Task(seed_candidate="seed", cases=[{"id": "a"}, {"id": "b"}])
    ctx = EngineContext(
        reflection_lm=model,
        run_dir=str(tmp_path),
        progress_callback=lambda event, metrics: sink.append((event, metrics)),
    )
    result = BestOfNEngine().run(task, server, ctx)

    candidates = [metrics for event, metrics in sink if event == PROGRESS_CANDIDATE]
    assert [row["candidate_id"] for row in candidates] == ["0", "1"]
    assert all(row["parent_id"] is None and row["generation"] == 0 for row in candidates)
    assert candidates[0]["prompt"] == {"current_candidate": "aeiou"}
    assert candidates[0]["per_example"] == [{"id": "0", "score": 1.0}, {"id": "1", "score": 1.0}]
    assert candidates[0]["score"] == pytest.approx(result.best_score)
    assert candidates[1]["score"] == pytest.approx(0.0)
    assert [row["discovered_at_evals"] for row in candidates] == [2, 4]
    assert server.used == 5


def _crash_on(text: str) -> Any:
    """Build a vowel scorer that raises for one candidate text.

    Args:
        text: Candidate text that makes the scorer raise.

    Returns:
        A scorer with the ``vowel_scorer`` contract.
    """

    def score(candidate: Any, example: Any = None) -> tuple[float, dict[str, Any]]:
        """Raise for the crash text, else score vowel density.

        Args:
            candidate: Proposal.
            example: Dataset example.

        Returns:
            Vowel density and side info.

        Raises:
            RuntimeError: For the crash text.
        """
        if candidate == text:
            raise RuntimeError("scorer bug")
        return vowel_scorer(candidate, example)

    return score


def test_best_of_n_skips_a_sample_whose_metric_raises(tmp_path: Path) -> None:
    """A raising metric neither crashes Best-of-N nor wins, and valid samples keep their order (SKY-367)."""
    model = _SequenceModel(["broken", "aeiou", "xyz"])
    sink: list[tuple[str, dict[str, Any]]] = []
    server = EvalServer(_crash_on("broken"), max_evals=6)
    task = Task(seed_candidate="seed", cases=[{"id": "a"}, {"id": "b"}])
    ctx = EngineContext(
        reflection_lm=model,
        run_dir=str(tmp_path),
        progress_callback=lambda event, metrics: sink.append((event, metrics)),
    )

    result = BestOfNEngine().run(task, server, ctx)

    assert result.best_candidate == "aeiou"
    assert result.best_score == 1.0
    log = result.metadata["bon_cost_log"]
    assert all(entry["score"] is None or math.isfinite(entry["score"]) for entry in log)
    assert [entry["score"] for entry in log] == [None, 1.0, 0.0]
    assert [entry["best_score"] for entry in log] == [None, 1.0, 1.0]
    events = [metrics for event, metrics in sink if event == PROGRESS_CANDIDATE]
    assert [row["candidate_id"] for row in events] == ["1", "2"]
    assert [row["prompt"]["current_candidate"] for row in events] == ["aeiou", "xyz"]
    assert [row["score"] for row in events] == [1.0, 0.0]


def test_best_of_n_does_not_stop_on_a_sample_with_one_raising_case(tmp_path: Path) -> None:
    """A sample with one raising case is unscored, so it cannot reach ``stop_at_score`` (SKY-367)."""
    model = _SequenceModel(["abab", "aeiou", "xyz"])
    sink: list[tuple[str, dict[str, Any]]] = []
    task = Task(seed_candidate="seed", cases=[{"id": "a"}, {"id": "b"}])

    def score(candidate: Any, example: Any = None) -> tuple[float, dict[str, Any]]:
        """Raise on the second case of the first sample only.

        Args:
            candidate: Proposal.
            example: Dataset example.

        Returns:
            Vowel density and side info.

        Raises:
            RuntimeError: For the first sample's second case.
        """
        if candidate == "abab" and example is task.cases[1]:
            raise RuntimeError("scorer bug")
        return vowel_scorer(candidate, example)

    server = EvalServer(score, max_evals=6)
    ctx = EngineContext(
        reflection_lm=model,
        run_dir=str(tmp_path),
        stop_at_score=0.4,
        progress_callback=lambda event, metrics: sink.append((event, metrics)),
    )

    result = BestOfNEngine().run(task, server, ctx)

    assert result.best_candidate == "aeiou"
    assert [entry["score"] for entry in result.metadata["bon_cost_log"]] == [None, 1.0]
    assert server.metric_error("abab") is not None
    assert server.used == 4
    events = [metrics for event, metrics in sink if event == PROGRESS_CANDIDATE]
    assert [row["prompt"]["current_candidate"] for row in events] == ["aeiou"]


def test_gepa_engine_survives_a_raising_metric_and_never_picks_it(tmp_path: Path) -> None:
    """A real GEPA run with a raising metric finishes, and the crashed text is never best (SKY-367)."""
    # The fake LM first proposes "aeiou": make that the crashed version.
    server = EvalServer(_crash_on("aeiou"), max_evals=16)
    task = Task(seed_candidate="hello world", objective="more vowels", cases=[{"i": 0}, {"i": 1}, {"i": 2}])

    result = GepaEngine().run(task, server, make_ctx(str(tmp_path)))

    assert server.used <= 16
    assert result.best_candidate != "aeiou"
    assert result.best_score is not None
    assert math.isfinite(result.best_score)
    tree = result.metadata["candidate_tree"]
    assert all(node["candidate"] != "aeiou" for node in tree)
    assert all(node["val_score"] is None or math.isfinite(node["val_score"]) for node in tree)


def test_gepa_engine_ranks_by_scored_cases_when_one_case_always_crashes(tmp_path: Path) -> None:
    """A validation case that crashes for every version leaves GEPA ranking by the scored cases (SKY-367)."""

    def crash_on_case_two(candidate: Any, case: Any = None) -> tuple[float, dict[str, Any]]:
        """Raise on case 2 for every version, else score vowel density.

        Args:
            candidate: Candidate text.
            case: The case.

        Returns:
            Vowel density and side information.

        Raises:
            RuntimeError: On case 2.
        """
        if case == {"i": 2}:
            raise RuntimeError("scorer bug")
        return vowel_scorer(candidate, case)

    server = EvalServer(crash_on_case_two, max_evals=16)
    task = Task(seed_candidate="hello world", objective="more vowels", cases=[{"i": 0}, {"i": 1}, {"i": 2}])

    result = GepaEngine().run(task, server, make_ctx(str(tmp_path)))

    assert server.used <= 16
    # The fake LM proposes "aeiou" first: GEPA must accept it over the seed.
    assert result.best_candidate == "aeiou"
    tree = result.metadata["candidate_tree"]
    assert [node["candidate"] for node in tree[:2]] == ["hello world", "aeiou"]
    seed_score, child_score = (node["val_score"] for node in tree[:2])
    assert math.isfinite(seed_score)
    assert child_score > seed_score
    assert server.metric_error("aeiou") is not None


def _crash_off_seed(candidate: Any, case: Any = None) -> tuple[float, dict[str, Any]]:
    """Raise on cases 1 and 2 for every version but the seed, else score vowel density.

    Args:
        candidate: Candidate text.
        case: The case.

    Returns:
        Vowel density and side information.

    Raises:
        RuntimeError: On cases 1 and 2 of a non-seed version.
    """
    if candidate != "hello world" and case is not None and case["i"] != 0:
        raise RuntimeError("scorer bug")
    return vowel_scorer(candidate, case)


def test_gepa_engine_does_not_stop_on_a_partly_crashed_version(tmp_path: Path) -> None:
    """A version with a crashed case cannot reach ``stop_at_score`` on its filled mean (SKY-367).

    Args:
        tmp_path: Artifact directory.
    """
    server = EvalServer(_crash_off_seed, max_evals=30)
    task = Task(seed_candidate="hello world", objective="more vowels", cases=[{"i": 0}, {"i": 1}, {"i": 2}])

    GepaEngine().run(task, server, make_ctx(str(tmp_path), stop_at_score=0.9))

    assert server.metric_error("aeiou") is not None
    assert server.used == 30


def test_gepa_engine_live_event_shows_no_score_for_a_partly_crashed_version(tmp_path: Path) -> None:
    """A live candidate event carries no filled mean for a version with a crashed case (SKY-367).

    Args:
        tmp_path: Artifact directory.
    """
    sink: list[tuple[str, dict[str, Any]]] = []
    server = EvalServer(_crash_off_seed, max_evals=12)
    task = Task(seed_candidate="hello world", objective="more vowels", cases=[{"i": 0}, {"i": 1}, {"i": 2}])

    GepaEngine().run(task, server, make_ctx(str(tmp_path), progress_callback=lambda e, m: sink.append((e, m))))

    events = {m["candidate_id"]: m for e, m in sink if e == PROGRESS_CANDIDATE}
    assert math.isfinite(events["0"]["score"])
    assert len(events["0"]["per_example"]) == 3
    assert (events["1"]["score"], events["1"]["per_example"]) == (None, [])


def test_auto_lane_hands_off_the_seed_at_minus_inf_when_the_engine_returns_a_crashed_one(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """An Auto lane never hands upstream a crashed version or a minibatch-only mean (SKY-367).

    Args:
        tmp_path: Artifact directory.
        monkeypatch: Replaces the lane's engine.
    """

    class _FilledMeanEngine:
        """Score two versions and return the crashed one, as an engine ranking by filled means would."""

        def run(self, task: Task, server: EvalServer, ctx: EngineContext) -> Result:
            """Score "axyz" fully, "eeee" on case 0 only, crash "aeiou" on case 1, and return "aeiou".

            Args:
                task: The lane task.
                server: The lane server.
                ctx: Unused.

            Returns:
                The crashed version at its filled mean.
            """
            server.evaluate("eeee", task.cases[0])
            for candidate in ("axyz", "aeiou"):
                for case in task.cases:
                    server.evaluate(candidate, case)
            return Result(best_candidate="aeiou", best_score=1.0, total_evals=server.used)

    def scorer(candidate: Any, case: Any = None) -> tuple[float, dict[str, Any]]:
        """Raise on case 1 of "aeiou", else score vowel density.

        Args:
            candidate: Candidate text.
            case: The case.

        Returns:
            Vowel density and side information.

        Raises:
            RuntimeError: On case 1 of "aeiou".
        """
        if candidate == "aeiou" and case == {"i": 1}:
            raise RuntimeError("scorer bug")
        return vowel_scorer(candidate, case)

    monkeypatch.setattr(auto, "get_engine", lambda name, caps: _FilledMeanEngine())
    lanes: list[auto.LaneOutcome] = []
    lane_engine = auto._LaneEngine(
        "best_of_n", "explore", make_ctx(str(tmp_path)), _NATIVE_CAPS, lanes, None, itertools.count()
    )
    upstream = SimpleNamespace(evaluate=scorer, budget=SimpleNamespace(remaining=10))

    task = UpstreamTask("lane", seed_candidate="seed", train_set=[{"i": 0}, {"i": 1}])
    result = lane_engine.run(task, upstream)  # type: ignore[arg-type]

    assert (result.best_candidate, result.best_score) == ("seed", -math.inf)
    assert (lanes[0].best_candidate, lanes[0].best_score) == ("seed", -math.inf)


def test_auto_evaluator_gives_upstream_unscored_and_keeps_the_metric_error(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Auto's upstream evaluator turns a raising metric into UNSCORED; Skynet keeps the metric_error (SKY-367).

    Args:
        tmp_path: Artifact directory.
        monkeypatch: Replaces the upstream composition to capture its evaluator.
    """

    class _CapturedError(Exception):
        """Stop the run once the evaluator is captured."""

    def capture(**kwargs: Any) -> Any:
        """Capture the evaluator that Auto gives upstream.

        Args:
            kwargs: The upstream composition arguments.

        Raises:
            _CapturedError: Always, with the evaluator.
        """
        raise _CapturedError(kwargs["evaluator"])

    def crash(candidate: Any, case: Any = None) -> tuple[float, dict[str, Any]]:
        """Raise on the version "broken", else score vowel density.

        Args:
            candidate: Candidate text.
            case: The case.

        Returns:
            Vowel density and side information.

        Raises:
            RuntimeError: On the version "broken".
        """
        if candidate == "broken":
            raise RuntimeError("scorer bug")
        return vowel_scorer(candidate, case)

    monkeypatch.setattr(auto, "optimize_best_of", capture)
    server = EvalServer(crash, max_evals=2 * (len(AUTO_ENGINES) + 1))
    with pytest.raises(_CapturedError) as captured:
        auto.run_strategy(BlackboxStrategy(), Task("seed"), server, make_ctx(str(tmp_path)), caps=_NATIVE_CAPS)
    evaluator = captured.value.args[0]

    assert evaluator("broken")[0] == UNSCORED
    assert evaluator("aeiou")[0] == 1.0
    assert server.metric_error("broken") is not None
    assert server.metric_error("aeiou") is None


@pytest.mark.parametrize("seed", ["seed", None])
@pytest.mark.parametrize("budget", [0, 1])
def test_best_of_n_does_not_invent_a_seed_score(tmp_path: Path, seed: str | None, budget: int) -> None:
    """Preserve unscored seeds and reject seedless runs without a completed candidate."""
    model = _SequenceModel(["proposed"])
    server = EvalServer(vowel_scorer, max_evals=budget)
    task = Task(seed_candidate=seed, cases=[{"id": "a"}, {"id": "b"}])
    context = EngineContext(reflection_lm=model, run_dir=str(tmp_path))
    if seed is None:
        with pytest.raises(ServiceError, match="stopped before producing a fully evaluated candidate"):
            BestOfNEngine().run(task, server, context)
    else:
        result = BestOfNEngine().run(task, server, context)
        assert result.best_candidate == seed
        assert result.best_score is None
        assert result.total_evals == budget
        assert result.metadata["bon_cost_log"] == []

    assert server.used == budget
    assert len(model.messages) == budget


def test_best_of_n_stops_at_target_score(tmp_path: Path) -> None:
    """Let upstream stop once a fully evaluated sample meets the requested score."""
    model = _SequenceModel(["aeiou", "must not be requested"])
    server = EvalServer(vowel_scorer, max_evals=10)
    result = BestOfNEngine().run(
        Task(seed_candidate="seed"),
        server,
        EngineContext(reflection_lm=model, run_dir=str(tmp_path), stop_at_score=0.9),
    )
    assert result.best_score == 1.0
    assert server.used == len(model.messages) == 1


def test_best_of_n_refuses_multi_part_seeds(tmp_path: Path) -> None:
    """Reject unsupported named components before any model or evaluation call."""
    with pytest.raises(ServiceError, match="text starting points only"):
        BestOfNEngine().run(
            Task(seed_candidate={"a": "b"}), EvalServer(vowel_scorer, max_evals=5), make_ctx(str(tmp_path))
        )


def test_best_of_n_uses_metered_model_and_propagates_dspy_callbacks(tmp_path: Path) -> None:
    """Keep optimization calls in the configured model's usage history and callback context."""
    callback = MagicMock(spec=BaseCallback)
    with FakeGateway(reply="```\naeiou\n```", usage=(7, 2)) as gateway:
        lm = dspy.LM("openai/metered-fixture", api_base=gateway.url, api_key="fixture-key", cache=False)
        ctx = EngineContext(reflection_lm=lambda messages: lm(messages=messages)[0], run_dir=str(tmp_path))
        with dspy.context(callbacks=[callback]):
            result = BestOfNEngine().run(Task(seed_candidate="seed"), EvalServer(vowel_scorer, max_evals=2), ctx)

    assert result.total_evals == 2
    assert len(gateway.requests) == len(lm.history) == 2
    assert callback.on_lm_end.call_count == 2
    assert sum(entry["usage"]["prompt_tokens"] for entry in lm.history) == 14
    assert sum(entry["usage"]["completion_tokens"] for entry in lm.history) == 4
    assert {request["authorization"] for request in gateway.requests} == {"Bearer fixture-key"}


def test_best_of_n_propagates_model_cost_stop(tmp_path: Path) -> None:
    """Propagate metering errors through the transport instead of reporting a successful seed."""
    lm = MagicMock(side_effect=CostCeilingExceededError("spending allowance reached"))
    with pytest.raises(CostCeilingExceededError, match="spending allowance reached"):
        BestOfNEngine().run(
            Task(seed_candidate="seed"),
            EvalServer(vowel_scorer, max_evals=2),
            EngineContext(reflection_lm=lm, run_dir=str(tmp_path)),
        )
    assert lm.call_count == 1


def test_gepa_engine_returns_seed_without_budget(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    """No budget means GEPA is never started."""
    optimize = MagicMock()
    monkeypatch.setattr(gepa_mod, "optimize_anything", optimize)

    result = GepaEngine().run(
        Task(seed_candidate="seed"), EvalServer(vowel_scorer, max_evals=0), make_ctx(str(tmp_path))
    )

    assert result.best_candidate == "seed"
    assert result.total_evals == 0
    optimize.assert_not_called()


def test_gepa_engine_routes_evaluations_and_unwraps_text_results(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """GEPA scores only through the server and its dict-shaped text winner is unwrapped."""
    seen: dict[str, Any] = {}

    def fake_optimize(**kwargs: Any) -> Any:
        """Score two versions through the evaluator and report the better one."""
        seen.update(kwargs)
        kwargs["evaluator"]("aaa", kwargs["dataset"][0])
        kwargs["evaluator"]("xxx", kwargs["dataset"][0])
        gepa_result = MagicMock()
        gepa_result.best_candidate = {"current_candidate": "aaa"}
        gepa_result.best_idx = 1
        gepa_result.val_aggregate_scores = [0.0, 1.0]
        gepa_result.candidates = [{"current_candidate": "seed"}, {"current_candidate": "aaa"}]
        gepa_result.parents = [[None], [0]]
        gepa_result.discovery_eval_counts = [1, 2]
        gepa_result.total_metric_calls = 2
        return gepa_result

    monkeypatch.setattr(gepa_mod, "optimize_anything", fake_optimize)
    server = EvalServer(vowel_scorer, max_evals=7)
    task = Task(seed_candidate="seed", objective="vowels", background="bg", cases=[{"i": 0}, {"i": 1}])

    result = GepaEngine().run(task, server, make_ctx(str(tmp_path), seed=3, stop_at_score=0.95))

    assert result.best_candidate == "aaa"
    assert result.best_score == 1.0
    assert result.total_evals == 2
    assert result.metadata == {
        "upstream_source": GEPA_SOURCE,
        "candidates": 2,
        "gepa_metric_calls": 2,
        "candidate_tree": [
            {"candidate": "seed", "parents": [None], "val_score": 0.0, "discovery_evals": 1},
            {"candidate": "aaa", "parents": [0], "val_score": 1.0, "discovery_evals": 2},
        ],
    }
    assert seen["seed_candidate"] == "seed"
    assert seen["objective"] == "vowels"
    assert seen["background"] == "bg"
    assert seen["dataset"] == seen["valset"] == [{"i": 0}, {"i": 1}]
    config = seen["config"]
    assert config.engine.max_metric_calls == 7
    assert config.engine.seed == 3
    assert config.engine.parallel is False
    assert config.engine.run_dir == str(tmp_path / "gepa")
    assert config.stop_callbacks is not None
    assert len(config.stop_callbacks) == 1
    assert Path(config.engine.run_dir).is_dir()


def test_gepa_engine_keeps_unscored_seed_without_checkpoint_evidence(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Do not promote raw case scores when a budget interruption leaves no completed checkpoint."""

    def fake_optimize(**kwargs: Any) -> Any:
        """Score until the server cuts the run off."""
        kwargs["evaluator"]("xxa")
        kwargs["evaluator"]("aaa")
        kwargs["evaluator"]("never scored")

    monkeypatch.setattr(gepa_mod, "optimize_anything", fake_optimize)
    monkeypatch.setattr(gepa_mod, "_load_result_from_state", lambda run_dir, *, seed, str_mode: None)

    result = GepaEngine().run(
        Task(seed_candidate="seed"), EvalServer(vowel_scorer, max_evals=2), make_ctx(str(tmp_path))
    )

    assert result.best_candidate == "seed"
    assert result.best_score is None
    assert result.total_evals == 2


def test_gepa_engine_recovers_checkpointed_state_after_budget_exhaustion(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """A checkpointed GEPA state is preferred over the raw server best."""

    def fake_optimize(**kwargs: Any) -> Any:
        """Die on the budget immediately."""
        raise BudgetExhaustedError("spent")

    recovered = MagicMock()
    recovered.best_candidate = "from-state"
    recovered.best_idx = 0
    recovered.val_aggregate_scores = [0.4]
    recovered.candidates = [{}]
    recovered.total_metric_calls = 9
    monkeypatch.setattr(gepa_mod, "optimize_anything", fake_optimize)
    monkeypatch.setattr(gepa_mod, "_load_result_from_state", lambda run_dir, *, seed, str_mode: recovered)

    result = GepaEngine().run(
        Task(seed_candidate="seed"), EvalServer(vowel_scorer, max_evals=2), make_ctx(str(tmp_path))
    )

    assert result.best_candidate == "from-state"
    assert result.best_score == 0.4


def test_gepa_engine_real_run_improves_the_seed(tmp_path: Path) -> None:
    """The real ``optimize_anything`` loop runs against the eval server and stays within budget."""
    server = EvalServer(vowel_scorer, max_evals=12)
    task = Task(seed_candidate="hello world", objective="more vowels", cases=[{"i": 0}, {"i": 1}, {"i": 2}])

    result = GepaEngine().run(task, server, make_ctx(str(tmp_path)))

    assert server.used <= 12
    assert result.best_score is not None
    assert vowel_scorer(result.best_candidate)[0] >= vowel_scorer("hello world")[0]
    assert (tmp_path / "gepa").is_dir()
    tree = result.metadata["candidate_tree"]
    assert tree[0]["candidate"] == "hello world"
    assert tree[0]["parents"] == [None]
    assert all(parent is None or 0 <= parent < index for index, node in enumerate(tree) for parent in node["parents"])
    assert any(node["candidate"] == result.best_candidate for node in tree)


def test_gepa_engine_streams_candidates_while_running(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    """Candidates checkpointed to ``gepa_state.bin`` reach the progress sink as live events."""

    def fake_optimize(**kwargs: Any) -> Any:
        """Checkpoint a two-candidate state the way GEPA's engine does, then finish."""
        state = {
            "program_candidates": [{"current_candidate": "seed"}, {"current_candidate": "aaa"}],
            "parent_program_for_candidate": [[None], [0]],
            "prog_candidate_val_subscores": [{"0": 0.0}, {"0": 1.0}],
            "num_metric_calls_by_discovery": [1, 2],
            "full_program_trace": [{"i": 0, "new_program_idx": 1}],
        }
        (Path(kwargs["config"].engine.run_dir) / "gepa_state.bin").write_bytes(pickle.dumps(state))
        gepa_result = MagicMock()
        gepa_result.best_candidate = {"current_candidate": "aaa"}
        gepa_result.best_idx = 1
        gepa_result.val_aggregate_scores = [0.0, 1.0]
        gepa_result.candidates = [{"current_candidate": "seed"}, {"current_candidate": "aaa"}]
        gepa_result.parents = [[None], [0]]
        gepa_result.discovery_eval_counts = [1, 2]
        gepa_result.total_metric_calls = 2
        return gepa_result

    monkeypatch.setattr(gepa_mod, "optimize_anything", fake_optimize)
    sink: list[tuple[str, dict[str, Any]]] = []
    ctx = make_ctx(str(tmp_path), progress_callback=lambda event, metrics: sink.append((event, metrics)))

    result = GepaEngine().run(Task(seed_candidate="seed"), EvalServer(vowel_scorer, max_evals=5), ctx)

    assert result.best_candidate == "aaa"
    candidates = [metrics for event, metrics in sink if event == PROGRESS_CANDIDATE]
    assert [c["candidate_id"] for c in candidates] == ["0", "1"]
    assert candidates[0]["parent_id"] is None
    assert candidates[0]["prompt"] == {"current_candidate": "seed"}
    assert candidates[1]["parent_id"] == "0"
    assert candidates[1]["generation"] == 1
    assert candidates[1]["score"] == 1.0
    assert candidates[1]["iteration"] == 0


def test_gepa_engine_streams_scorer_feedback(monkeypatch, tmp_path) -> None:
    """Each scorer call inside GEPA is forwarded as a mini-batch feedback event keyed by validation index."""
    sink: list[tuple[str, dict]] = []

    def fake_optimize_anything(**kwargs):
        """Score the seed against the second case once, then hand back a minimal result.

        Args:
            **kwargs: The engine's ``optimize_anything`` call.

        Returns:
            A result shaped like GEPA's.
        """
        kwargs["evaluator"](kwargs["seed_candidate"], kwargs["dataset"][1])
        result = MagicMock()
        result.best_candidate = {"current_candidate": "seed"}
        result.best_idx = 0
        result.val_aggregate_scores = [0.5]
        result.candidates = [{"current_candidate": "seed"}]
        result.parents = [[None]]
        result.discovery_eval_counts = [1]
        result.total_metric_calls = 1
        return result

    monkeypatch.setattr(gepa_mod, "optimize_anything", fake_optimize_anything)
    ctx = make_ctx(str(tmp_path), progress_callback=lambda event, metrics: sink.append((event, metrics)))
    cases = [{"q": 1}, {"q": 2}]

    GepaEngine().run(Task(seed_candidate="seed", cases=cases), EvalServer(vowel_scorer, max_evals=5), ctx)

    assert [m for e, m in sink if e == PROGRESS_MINIBATCH] == [
        {
            "example_id": "1",
            "score": 0.5,
            "feedback": "vowels: 2",
            "prediction": "",
            "iteration": None,
            "images": [],
            "images_dropped": 0,
        }
    ]


def _named_scorer(candidate: Any, case: Any = None) -> tuple[float, dict[str, Any]]:
    """Score with feedback and two named scores, each with its own feedback.

    Args:
        candidate: Proposed text.
        case: Ignored.

    Returns:
        The vowel density, its feedback and the named scores.
    """
    score, _ = vowel_scorer(candidate, case)
    return score, {
        "feedback": "needs more vowels",
        "scores": {
            "vowels": {"score": score, "feedback": "too few vowels"},
            "length": {"score": 0.5, "feedback": "a bit short"},
        },
    }


@pytest.mark.parametrize("cases", [[], [{"i": 0}], [{"i": 0}, {"i": 1}, {"i": 2}]])
def test_gepa_engine_runs_with_zero_one_or_many_cases_and_shapes_named_scores(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, cases: list[dict[str, int]]
) -> None:
    """Cases go in as both dataset and valset; none means single-instance mode; named scores reach GEPA.

    GEPA's objective frontier reads ``side_info["scores"]`` as numbers, so the
    named scores arrive as plain numbers with their feedback beside them for
    the reflection prompt.

    Args:
        tmp_path: Workspace.
        monkeypatch: Replaces upstream ``optimize_anything``.
        cases: The run's cases.
    """
    seen: dict[str, Any] = {}
    returned: list[tuple[float, dict[str, Any]]] = []

    def fake_optimize(**kwargs: Any) -> Any:
        """Score one version through the evaluator on each case, or once without cases."""
        seen.update(kwargs)
        returned.extend(kwargs["evaluator"]("aaa", example) for example in kwargs.get("dataset") or [None])
        gepa_result = MagicMock()
        gepa_result.best_candidate = {"current_candidate": "aaa"}
        gepa_result.best_idx = 0
        gepa_result.val_aggregate_scores = [1.0]
        gepa_result.candidates = [{"current_candidate": "aaa"}]
        gepa_result.parents = [[None]]
        gepa_result.discovery_eval_counts = [1]
        gepa_result.total_metric_calls = len(returned)
        return gepa_result

    monkeypatch.setattr(gepa_mod, "optimize_anything", fake_optimize)
    server = EvalServer(_named_scorer, max_evals=5)

    result = GepaEngine().run(Task(seed_candidate="seed", cases=cases), server, make_ctx(str(tmp_path)))

    assert result.best_candidate == "aaa"
    assert server.used == max(1, len(cases))
    if cases:
        assert seen["dataset"] == seen["valset"] == cases
    else:
        assert "dataset" not in seen
        assert "valset" not in seen
    score, side_info = returned[0]
    assert side_info["scores"] == {"vowels": score, "length": 0.5}
    assert side_info["Feedback per score"] == {"vowels": "too few vowels", "length": "a bit short"}
    assert side_info["feedback"] == "needs more vowels"
