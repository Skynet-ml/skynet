"""Verify held-out cases never reach a repository run's engine during the search (SKY-649).

Each test marks the held-out cases' inputs and expected values with unique
strings, runs a repository job through the service, and greps everything the
engine and the search-phase run log receive for those strings. Every engine
runs on the worker engine loop (SKY-728), so the engine's task is all that it
can pass to a proposer: the loop engines' own tests check what reaches a box.
"""

from __future__ import annotations

import json
import logging
import subprocess
import threading
import time
from pathlib import Path
from typing import Any

import pytest

from core.constants import PROGRESS_EVALUATION_STARTED, PROGRESS_LANE_COMPLETED
from core.models.blackbox import BlackboxRunRequest
from core.models.common import SplitFractions

from ...unified.split import CaseSplits, split_cases
from .. import repo_tree
from .. import service as service_mod
from ..autoresearch import AutoResearchEngine
from ..autosaddler import AutoSaddlerEngine
from ..best_of_n import BestOfNEngine
from ..gepa_engine import GepaEngine
from ..harness import GatewayConfig
from ..meta_harness import MetaHarnessEngine
from ..protocol import Result, Task
from ..shinka_evolve import ShinkaEvolveEngine
from .mocks import FakeReflectionLM

HELDOUT_INPUT = "HELDOUT_INPUT_c41d7e"
HELDOUT_EXPECTED = "HELDOUT_EXPECTED_9b02fa"
MARKERS = (HELDOUT_INPUT, HELDOUT_EXPECTED)
_PATCH = "diff --git a/src/app.py b/src/app.py\n--- a/src/app.py\n+++ b/src/app.py\n@@ -1 +1 @@\n-x = 1\n+x = 2\n"
_ENGINE_CLASSES = (
    GepaEngine,
    BestOfNEngine,
    AutoResearchEngine,
    MetaHarnessEngine,
    AutoSaddlerEngine,
    ShinkaEvolveEngine,
)


def _cases() -> list[dict[str, Any]]:
    """Build eight cases; the last two carry the held-out markers.

    Returns:
        Six plain cases, then two cases whose input and expected value hold a marker.
    """
    plain = [{"input": {"q": f"train {n}"}, "expected": f"answer {n}"} for n in range(6)]
    held = [{"input": {"q": f"{HELDOUT_INPUT} {n}"}, "expected": f"{HELDOUT_EXPECTED} {n}"} for n in range(2)]
    return plain + held


def _splits() -> CaseSplits:
    """Split the cases in order so the two marked cases are the held-out test split.

    Returns:
        Four train, two validation and two held-out cases.
    """
    splits = split_cases(_cases(), "pairs", SplitFractions(train=0.5, val=0.25, test=0.25), shuffle=False, seed=0)
    assert [HELDOUT_INPUT in json.dumps(case) for case in splits.test] == [True, True]
    assert not any(marker in json.dumps([*splits.train, *splits.val]) for marker in MARKERS)
    return splits


def _is_heldout(case: Any) -> bool:
    """Report whether a case is one of the marked held-out cases.

    Args:
        case: A case the scorer received.

    Returns:
        True when the case holds a marker.
    """
    return any(marker in json.dumps(case, default=str) for marker in MARKERS)


def _assert_no_marker(label: str, text: str) -> None:
    """Fail when ``text`` holds a held-out marker.

    Args:
        label: What the text is, for the failure message.
        text: Text the engine or the search-phase run log can see.
    """
    for marker in MARKERS:
        assert marker not in text, f"held-out marker {marker} reached {label}"


class _Scorer:
    """Score every case the same and record which cases arrive before and after the search."""

    usage = None

    def __init__(self) -> None:
        """Start with no recorded calls."""
        self.calls: list[tuple[Any, Any]] = []
        self.search_done = threading.Event()
        self.lock = threading.Lock()

    def __call__(self, candidate: Any, case: Any = None) -> tuple[float, dict[str, Any]]:
        """Record the call and return a fixed score.

        Args:
            candidate: The version to score.
            case: The case to score it on.

        Returns:
            A score that improves on any patch, with short feedback.
        """
        with self.lock:
            self.calls.append((self.search_done.is_set(), case))
        return (0.75 if candidate else 0.5), {"feedback": "scored"}

    def close(self) -> None:
        """Release nothing; the service calls this when the run ends."""


class _EngineRecorder:
    """Stand in for each engine's run: record the task it gets and score a patch on each of its cases."""

    def __init__(self) -> None:
        """Start with no recorded tasks."""
        self.tasks: list[tuple[str, Task]] = []
        self.lock = threading.Lock()

    def run(self, name: str, task: Task, server: Any) -> Result:
        """Record the engine's task and score the patch on every case it holds, within the budget.

        Args:
            name: The engine's id.
            task: The task the service gives the engine.
            server: The engine's scorer bridge.

        Returns:
            The patch as the engine's best version.
        """
        with self.lock:
            self.tasks.append((name, task))
        scores = []
        for case in task.cases or [None]:
            if server.remaining <= 0:
                break
            score, _ = server.evaluate(_PATCH, case)
            scores.append(score)
        return Result(
            best_candidate=_PATCH,
            best_score=sum(scores) / len(scores) if scores else 0.75,
            total_evals=server.used,
            metadata={"engine": name},
        )


def _repo_snapshot(tmp_path: Path) -> dict[str, Any]:
    """Pack a one-file repository into upload chunks, as the parent stages it.

    Args:
        tmp_path: Where to put the tree, its archive and its chunks.

    Returns:
        The snapshot block the service passes to the engines.
    """
    source = tmp_path / "source"
    (source / "src").mkdir(parents=True)
    (source / "src" / "app.py").write_text("x = 1\n")
    archive = tmp_path / "tree.tgz"
    subprocess.run(["tar", "-czf", str(archive), "-C", str(source), "."], check=True)
    chunks = []
    for index, chunk in enumerate(repo_tree.archive_chunks(archive)):
        chunks.append(tmp_path / f"tree.{index:04d}.b64")
        chunks[-1].write_text(chunk, encoding="ascii")
    return {"chunks": [str(chunk) for chunk in chunks], "readonly_paths": [], "constrained": False}


def _request(strategy: dict[str, Any]) -> BlackboxRunRequest:
    """Build a repository run request.

    Args:
        strategy: The run's strategy block.

    Returns:
        The validated request.
    """
    return BlackboxRunRequest.model_validate(
        {
            "recipe": "repo",
            "scorer": {"kind": "python", "metric_code": "def score(repo_path):\n    return 1.0\n"},
            "reflection_model_config": {"name": "fake/model"},
            "budget": {"max_scorer_runs": 12},
            "strategy": strategy,
            "max_cost_cents": 100,
            "target": {"kind": "repo", "repo": {"repository": "octo/hello", "editable_paths": ["src"]}},
        }
    )


def _run(
    request: BlackboxRunRequest,
    recorder: _EngineRecorder,
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    caplog: pytest.LogCaptureFixture,
) -> tuple[Any, _Scorer, list[tuple[str, dict[str, Any]]], list[logging.LogRecord]]:
    """Run a repository job through the service with every engine's run recorded.

    Args:
        request: The repository run request.
        recorder: Replaces every engine's run.
        tmp_path: Per-test directory.
        monkeypatch: Replaces the model, the gateway and each engine's run.
        caplog: Captures the run log.

    Returns:
        The response, the scorer, every progress event, and the log records made before the final run.
    """
    monkeypatch.setattr(service_mod, "build_language_model", lambda config, *, disable_cache=False: FakeReflectionLM())
    monkeypatch.setattr(
        service_mod, "gateway_from_settings", lambda _settings: GatewayConfig("https://unused.example/v1", "test-key")
    )
    for engine_class in _ENGINE_CLASSES:
        monkeypatch.setattr(engine_class, "run", lambda self, task, server, ctx: recorder.run(self.name, task, server))
    caplog.set_level(logging.DEBUG)
    scorer = _Scorer()
    events: list[tuple[str, dict[str, Any]]] = []
    search_records: list[logging.LogRecord] = []

    def progress(event: str, metrics: dict[str, Any]) -> None:
        """Record each event and mark where the search ends.

        Args:
            event: The progress event name.
            metrics: The event's payload.
        """
        if event == PROGRESS_EVALUATION_STARTED and not scorer.search_done.is_set():
            search_records.extend(caplog.records)
            scorer.search_done.set()
        events.append((event, dict(metrics)))

    response = service_mod.run_job(
        service_mod.job_spec_from_payload(request),
        scorer,
        _splits(),
        artifact_id="sky-649",
        started=time.perf_counter(),
        progress_callback=progress,
        gepa_log_dir_path=str(tmp_path / "run"),
        repo_snapshot=_repo_snapshot(tmp_path),
    )
    assert scorer.search_done.is_set()
    return response, scorer, events, search_records


def _assert_search_is_clean(
    recorder: _EngineRecorder,
    scorer: _Scorer,
    events: list[tuple[str, dict[str, Any]]],
    search_records: list[logging.LogRecord],
) -> None:
    """Assert no held-out marker reached an engine or the search-phase run data, and the final run used them.

    Args:
        recorder: The recorded engine tasks.
        scorer: The run's scorer.
        events: Every progress event of the run.
        search_records: Log records made before the final run.
    """
    assert recorder.tasks
    # The train cases do reach the engine, so the scan below reads where case data lands.
    assert any("train 0" in json.dumps(task.cases, default=str) for _, task in recorder.tasks)
    for name, task in recorder.tasks:
        _assert_no_marker(f"engine {name} task", json.dumps(vars(task), default=str))
    search_events = events[: next(i for i, (e, _) in enumerate(events) if e == PROGRESS_EVALUATION_STARTED)]
    assert search_events
    _assert_no_marker("search progress events", json.dumps(search_events, default=str))
    for record in search_records:
        _assert_no_marker("search run log", record.getMessage() + json.dumps(record.__dict__, default=str))
    search_cases = [case for after, case in scorer.calls if not after]
    assert search_cases
    assert not any(_is_heldout(case) for case in search_cases)
    assert sum(_is_heldout(case) for after, case in scorer.calls if after) >= 2


@pytest.mark.parametrize(
    "strategy",
    [
        {"mode": "single", "engine": "gepa"},
        {"mode": "single", "engine": "best_of_n"},
        {"mode": "single", "engine": "meta_harness"},
        {"mode": "single", "engine": "autoresearch"},
        {"mode": "single", "engine": "autosaddler"},
        {"mode": "single", "engine": "shinka_evolve"},
        {"mode": "auto"},
    ],
    ids=lambda strategy: strategy.get("engine", "auto"),
)
def test_heldout_cases_never_reach_any_engine(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, caplog: pytest.LogCaptureFixture, strategy: dict[str, Any]
) -> None:
    """Every engine's task, its scoring calls and the search-phase run data hold no held-out case.

    Args:
        tmp_path: Per-test directory for the repository.
        monkeypatch: Replaces the model, the gateway and each engine's run.
        caplog: Captures the run log.
        strategy: The engine or Auto.
    """
    recorder = _EngineRecorder()

    response, scorer, events, search_records = _run(_request(strategy), recorder, tmp_path, monkeypatch, caplog)

    _assert_search_is_clean(recorder, scorer, events, search_records)
    if strategy["mode"] == "auto":
        # A failed lane runs no engine work, so the scan above would not cover it.
        lanes = [metrics for event, metrics in events if event == PROGRESS_LANE_COMPLETED]
        assert lanes
        assert all(lane["status"] == "completed" for lane in lanes), lanes
    assert response.split_counts.test == 2
    assert response.case_count == 2


def test_the_check_fails_when_a_heldout_case_enters_an_engine_task(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, caplog: pytest.LogCaptureFixture
) -> None:
    """Put one held-out case into the engine's task and confirm the check catches it.

    Args:
        tmp_path: Per-test directory for the repository.
        monkeypatch: Replaces the model, the gateway and each engine's run.
        caplog: Captures the run log.
    """
    recorder = _EngineRecorder()

    _, scorer, events, search_records = _run(
        _request({"mode": "single", "engine": "gepa"}), recorder, tmp_path, monkeypatch, caplog
    )
    name, task = recorder.tasks[0]
    task.cases = [*task.cases, _cases()[-2]]

    with pytest.raises(AssertionError, match=f"held-out marker {HELDOUT_INPUT} reached engine {name} task"):
        _assert_search_is_clean(recorder, scorer, events, search_records)
