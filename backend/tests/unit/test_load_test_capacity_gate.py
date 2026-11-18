"""The SKY-727 load-test harness: thread caps, the swarm gate, shard failures and the resume check."""

import asyncio
import queue
import sys
import threading
import time
from concurrent.futures import ThreadPoolExecutor
from contextlib import nullcontext
from dataclasses import replace
from datetime import UTC, datetime
from pathlib import Path
from types import SimpleNamespace
from typing import Any

import pytest
from sqlalchemy import create_engine, event, select
from sqlalchemy.engine import Engine
from sqlalchemy.exc import OperationalError
from sqlalchemy.orm import Session

from core.service_gateway.optimization.blackbox.sandbox_runtime import LocalSubprocessRuntime
from core.storage.models import AgentStepModel, JobModel, StoragePackModel, TreeObjectModel
from core.storage.object_store import LocalObjectStore
from core.storage.step_log_store import StepLogStore
from core.worker.engine_loop import EngineLoop
from load_tests import run_all
from load_tests.lib.metrics import ScenarioMetrics
from load_tests.scenarios import chaos_gate, reference_shapes, swarm_10k


def _no_start(*args: Any, **kwargs: Any) -> None:
    """Fail the test: a check that must refuse its config started real work.

    Args:
        *args: Not used.
        **kwargs: Not used.

    Raises:
        AssertionError: Always.
    """
    raise AssertionError("the check started real work")


def _swarm_config(**overrides: Any) -> swarm_10k.SwarmConfig:
    """Return a swarm config that reads no environment.

    Args:
        **overrides: The fields to change.

    Returns:
        The config.
    """
    fields: dict[str, Any] = {
        "agents": 10_000,
        "steps_per_agent": 10,
        "shards": 2,
        "flush_window_seconds": 0.5,
        "box_seconds": 30.0,
        "box_vcpus": 2,
        "box_steps_per_step": 0.75,
        "db_url": None,
    }
    fields.update(overrides)
    return swarm_10k.SwarmConfig(**fields)


def _pass_figures(shards: int, **overrides: Any) -> dict[str, Any]:
    """Return the figures of one swarm pass that meets every gate.

    Args:
        shards: The pass's shard count.
        **overrides: The figures to change.

    Returns:
        The figures, in the shape that ``run_pass`` returns.
    """
    figures: dict[str, Any] = {
        "shards": shards,
        "runs": 100,
        "runs_per_shard": [100 // shards] * shards,
        "commits": 100_000,
        "steps_stored": 100_000,
        "seconds_to_durable": 20.0,
        "steady_steps": 60_000,
        "steady_seconds": 10.0,
        "commits_per_second": 6_000.0,
        "step_wait_ms": {"mean": 500.0, "p50": 500.0, "p99": 900.0, "max": 950.0},
        "commit_call_ms": {"p50": 0.05, "p99": 1.0, "max": 4.0},
        "digest": "same",
    }
    figures.update(overrides)
    return figures


def _target_backend(monkeypatch: pytest.MonkeyPatch, config: swarm_10k.SwarmConfig) -> swarm_10k.SwarmConfig:
    """Name a Postgres database and a local S3 bucket, the backend that the swarm target needs.

    Args:
        monkeypatch: Sets the bucket.
        config: The swarm config.

    Returns:
        The config with a Postgres URL.
    """
    monkeypatch.setenv("LOAD_TEST_SWARM_S3_BUCKET", "swarm")
    return replace(config, db_url="postgresql://user@localhost/loadtest_swarm")


def _gate(
    monkeypatch: pytest.MonkeyPatch,
    config: swarm_10k.SwarmConfig,
    single: dict[str, Any] | None = None,
    **sharded: Any,
) -> list[str]:
    """Run the swarm gate on made-up pass figures, on the target backend, and return its violations.

    Args:
        monkeypatch: Replaces ``run_pass`` and sets the backend.
        config: The swarm config.
        single: The figures to change on the 1-shard pass, or ``None``.
        **sharded: The figures to change on the sharded pass.

    Returns:
        The gate's violations.
    """

    def fake_pass(config: swarm_10k.SwarmConfig, shard_count: int, folder: Path) -> dict[str, Any]:
        """Return made-up figures, with the changes on each pass.

        Args:
            config: The swarm config.
            shard_count: The pass's shard count.
            folder: Not used.

        Returns:
            The figures.
        """
        return _pass_figures(shard_count, **(sharded if shard_count > 1 else single or {}))

    monkeypatch.setattr(swarm_10k, "run_pass", fake_pass)
    result = swarm_10k.run(_target_backend(monkeypatch, config))
    assert result.slo_passed is (not result.slo_violations)
    return result.slo_violations


@pytest.mark.parametrize("agents", [0, 1, 31, 32, 33, 1_250, 10_000, 1_000_000])
def test_swarm_driver_threads_have_a_cap(agents: int) -> None:
    """Check that one swarm shard process starts at most the capped driver count.

    Args:
        agents: The shard's agents.
    """
    count = swarm_10k.driver_count(agents)
    assert 1 <= count <= swarm_10k.MAX_DRIVERS_PER_SHARD
    assert count <= max(1, agents)


@pytest.mark.parametrize("workers", [0, 1, 2, 8, 9, 10_000])
def test_chaos_pool_has_a_cap(workers: int) -> None:
    """Check that the chaos gate runs at most the capped number of runs at once.

    Args:
        workers: The asked number of runs at once.
    """
    assert 1 <= chaos_gate.pool_size(workers) <= chaos_gate.MAX_WORKERS


@pytest.mark.parametrize(
    ("overrides", "message"),
    [
        ({"shards": swarm_10k.MAX_SHARDS + 1}, "shard processes"),
        ({"agents": 0}, "at least 1 agent"),
        ({"steps_per_agent": 2}, "steps for each agent"),
    ],
)
def test_swarm_rejects_a_bad_config(monkeypatch: pytest.MonkeyPatch, overrides: dict[str, Any], message: str) -> None:
    """Check that the swarm refuses a config that it cannot gate, before it starts a shard process.

    Args:
        monkeypatch: Makes a started pass fail the test.
        overrides: The bad fields.
        message: A part of the error text.
    """
    monkeypatch.setattr(swarm_10k, "run_pass", _no_start)
    with pytest.raises(ValueError, match=message):
        swarm_10k.run(_swarm_config(**overrides))


@pytest.mark.parametrize(
    ("agents", "shards"),
    [(0, 2), (1, 2), (800, 2), (1_000, 2), (1_601, 3), (5_000, 7), (10_000, 13), (1_000_000, 32)],
)
def test_swarm_default_shards_follow_the_load(agents: int, shards: int) -> None:
    """Check that the default shard count is one shard for each 800 agents, from 2 to the cap.

    Args:
        agents: The swarm's agents.
        shards: The expected shard count.
    """
    assert swarm_10k.default_shards(agents) == shards


def test_swarm_shards_env_overrides_the_default(monkeypatch: pytest.MonkeyPatch) -> None:
    """Check that ``LOAD_TEST_SWARM_SHARDS`` overrides the default shard count.

    Args:
        monkeypatch: Sets the environment.
    """
    monkeypatch.setenv("LOAD_TEST_SWARM_AGENTS", "10000")
    monkeypatch.delenv("LOAD_TEST_SWARM_SHARDS", raising=False)
    assert swarm_10k.default_config().shards == 13
    monkeypatch.setenv("LOAD_TEST_SWARM_SHARDS", "8")
    assert swarm_10k.default_config().shards == 8


@pytest.mark.parametrize("agents", [1, 1_000, 5_700, 10_000])
def test_swarm_pace_offers_the_target_rate(agents: int) -> None:
    """Check that the paced agents together ask for more than the target rate, at seeded offsets in one interval.

    Args:
        agents: The swarm's agents.
    """
    interval = swarm_10k.step_interval(agents)
    assert agents / interval > swarm_10k.TARGET_COMMITS_PER_SECOND
    offsets = [swarm_10k.start_offset(f"agent-{i:05d}", interval) for i in range(50)]
    assert all(0 <= offset < interval for offset in offsets)
    assert len(set(offsets)) == len(offsets)
    assert offsets == [swarm_10k.start_offset(f"agent-{i:05d}", interval) for i in range(50)]


def test_run_all_leaves_the_swarm_out_by_default(monkeypatch: pytest.MonkeyPatch) -> None:
    """Check that a default ``run_all`` runs the other scenarios and not the swarm, which only runs when named.

    Args:
        monkeypatch: Sets the command line.
    """
    monkeypatch.setattr(sys, "argv", ["run_all"])
    names = run_all._parse_args().scenarios.split(",")
    assert "swarm_10k" not in names
    assert {"reference_shapes", "chaos_gate", "mixed_realistic"} <= set(names)


def test_swarm_gate_passes_good_figures(monkeypatch: pytest.MonkeyPatch) -> None:
    """Check that the gate passes a sharded pass that meets every limit.

    Args:
        monkeypatch: Replaces ``run_pass``.
    """
    assert _gate(monkeypatch, _swarm_config()) == []


def test_swarm_gate_fails_a_short_steady_part(monkeypatch: pytest.MonkeyPatch) -> None:
    """Check that a steady part of about 0 s fails the gate, also with a very high rate in it.

    Args:
        monkeypatch: Replaces ``run_pass``.
    """
    violations = _gate(
        monkeypatch, _swarm_config(), steady_steps=1, steady_seconds=0.0001, commits_per_second=999_999.9
    )
    assert len(violations) == 1
    assert "steady part" in violations[0]


@pytest.mark.parametrize(
    ("sharded", "text"),
    [
        ({"commits_per_second": 5_000.0}, "steady commit rate 5000.0/s over 60000 steady steps"),
        ({"step_wait_ms": {"mean": 900.0, "p50": 900.0, "p99": 1_200.0, "max": 1_300.0}}, "step wait p99"),
        ({"commit_call_ms": {"p50": 400.0, "p99": 500.0, "max": 510.0}}, "commit call p99"),
        ({"digest": "other"}, "different step logs"),
        ({"steps_stored": 99_999}, "stored 99999 of 100000"),
        ({"runs_per_shard": [100, 0]}, "a shard got no run"),
    ],
)
def test_swarm_gate_fails_each_broken_limit(
    monkeypatch: pytest.MonkeyPatch, sharded: dict[str, Any], text: str
) -> None:
    """Check that each broken limit of the sharded pass gives its own violation.

    Args:
        monkeypatch: Replaces ``run_pass``.
        sharded: The broken figures.
        text: A part of the violation text.
    """
    violations = _gate(monkeypatch, _swarm_config(), **sharded)
    assert len(violations) == 1
    assert text in violations[0]


def test_swarm_gate_fails_a_lost_step_on_the_one_shard_pass(monkeypatch: pytest.MonkeyPatch) -> None:
    """Check that the 1-shard pass also fails the gate when it stored fewer steps than it committed.

    Args:
        monkeypatch: Replaces ``run_pass``.
    """
    violations = _gate(monkeypatch, _swarm_config(), single={"steps_stored": 99_999})
    assert violations == ["1-shard pass stored 99999 of 100000"]


def test_swarm_gate_fails_when_the_layouts_are_not_compared(monkeypatch: pytest.MonkeyPatch) -> None:
    """Check that a swarm with 1 shard fails the gate, as it cannot compare two layouts.

    Args:
        monkeypatch: Replaces ``run_pass``.
    """
    violations = _gate(monkeypatch, _swarm_config(shards=1))
    assert violations == ["the shard layouts were not compared: 1 shard(s): the comparison needs at least 2"]


class _Process:
    """A stand-in for a shard process that has exited."""

    exitcode = -9

    def is_alive(self) -> bool:
        """Return that the process is gone.

        Returns:
            ``False``.
        """
        return False


def test_swarm_parent_fails_soon_when_a_shard_dies_with_no_report() -> None:
    """Check that the parent stops waiting when a shard process exits with no report."""
    started = time.monotonic()
    with pytest.raises(RuntimeError, match="exited with no report: swarm-a"):
        swarm_10k.collect_reports(queue.Queue(), {"swarm-a": _Process()}, timeout=8)
    assert time.monotonic() - started < 5


def test_swarm_parent_takes_a_report_from_a_shard_that_exited() -> None:
    """Check that a shard process that put its report and exited still counts."""
    results: queue.Queue[dict[str, Any]] = queue.Queue()
    results.put({"worker_id": "swarm-a", "waits": []})
    assert swarm_10k.collect_reports(results, {"swarm-a": _Process()}, timeout=60) == [
        {"worker_id": "swarm-a", "waits": []}
    ]


def test_swarm_driver_stops_when_the_shard_stops(tmp_path: Path) -> None:
    """Check that a driver returns soon after the shard sets its stop, in the middle of its steps.

    Args:
        tmp_path: The folder for the database and the objects.
    """
    engine = create_engine(f"sqlite:///{tmp_path / 'db.sqlite'}")
    run_id = swarm_10k.run_id_for("test", 0)
    swarm_10k._reset(engine, [run_id])
    store = StepLogStore(
        engine,
        LocalObjectStore(tmp_path / "objects"),
        "swarm-test-0",
        flush_window_seconds=0.01,
        flush_max_bytes=swarm_10k.SQLITE_FLUSH_MAX_BYTES,
    )
    stop = threading.Event()
    done: list[Any] = []
    config = _swarm_config(agents=1, steps_per_agent=1_000_000)
    driver = threading.Thread(
        target=lambda: done.append(
            swarm_10k._drive([(run_id, "agent-00000")], config, store, swarm_10k.FakeBoxProvider(), 0.0, stop)
        ),
        daemon=True,
    )
    try:
        driver.start()
        time.sleep(0.3)
        stop.set()
        driver.join(timeout=5)
        assert not driver.is_alive()
        assert 0 < len(done[0][0]) < config.steps_per_agent
    finally:
        stop.set()
        store.close()
        engine.dispose()


def test_swarm_shard_stops_its_drivers_when_one_fails(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
    """Check that a shard process reports a driver failure soon, and does not wait for its other drivers.

    Args:
        monkeypatch: Replaces ``_drive`` with a driver that waits for the stop and a driver that fails.
        tmp_path: The folder for the shard's database and objects.
    """

    def drive(agents: list[tuple[str, str]], *args: Any) -> list[Any]:
        """Wait for the stop as a long driver does, or fail at once.

        Args:
            agents: The driver's agents; agent 0 waits and the other one fails.
            *args: The other ``_drive`` arguments; the last one is the stop.

        Returns:
            No steps.

        Raises:
            RuntimeError: For the failed driver.
        """
        if agents[0][1] == "agent-0":
            args[-1].wait(10)
            return [[]]
        raise RuntimeError("driver failed")

    monkeypatch.setattr(swarm_10k, "_drive", drive)
    results: queue.Queue[dict[str, Any]] = queue.Queue()
    shard = {
        "url": f"sqlite:///{tmp_path / 'db.sqlite'}",
        "folder": str(tmp_path),
        "worker_id": "swarm-w",
        "flush_max_bytes": swarm_10k.SQLITE_FLUSH_MAX_BYTES,
    }
    started = time.monotonic()
    swarm_10k._shard_process(
        _swarm_config(agents=2), shard, [("run", "agent-0"), ("run", "agent-1")], threading.Barrier(1), results
    )
    assert time.monotonic() - started < 5
    report = results.get_nowait()
    assert "driver failed" in report["error"]


def test_swarm_driver_failure_stops_the_other_drivers(monkeypatch: pytest.MonkeyPatch) -> None:
    """Check that a failed driver stops an earlier driver, which ``pool.map`` would otherwise wait for.

    Args:
        monkeypatch: Replaces ``_drive`` with a driver that waits for the stop and a driver that fails.
    """

    def drive(agents: list[tuple[str, str]], *args: Any) -> list[Any]:
        """Wait for the stop as a long driver does, or fail at once.

        Args:
            agents: ``[("wait", "")]`` for the long driver, anything else for the failed one.
            *args: The other ``_drive`` arguments; the last one is the stop.

        Returns:
            Whether the stop was set before the wait ended.

        Raises:
            RuntimeError: For the failed driver.
        """
        if agents[0][0] == "wait":
            return [args[-1].wait(10)]
        raise RuntimeError("driver failed")

    monkeypatch.setattr(swarm_10k, "_drive", drive)
    stop = threading.Event()
    started = time.monotonic()
    with ThreadPoolExecutor(max_workers=2) as pool:
        results = pool.map(
            lambda group: swarm_10k._drive_or_stop(group, _swarm_config(), None, None, 0.0, stop),
            [[("wait", "")], [("fail", "")]],
        )
        assert next(results) == [True]
        with pytest.raises(RuntimeError, match="driver failed"):
            next(results)
    assert time.monotonic() - started < 5


def test_chaos_gate_rejects_a_kill_interval_of_zero(monkeypatch: pytest.MonkeyPatch) -> None:
    """Check that the chaos gate refuses a kill interval that would make its job spin.

    Args:
        monkeypatch: Makes a started run fail the test.
    """
    monkeypatch.setattr(chaos_gate, "box_runtime", _no_start)
    config = chaos_gate.ChaosGateConfig(
        runs_per_shape=1, rounds=1, workers=1, kill_interval_seconds=0.0, seed=1, model_wait_seconds=1.0
    )
    with pytest.raises(ValueError, match="kill interval"):
        chaos_gate.run(config)


def test_work_log_allows_a_repeat_only_for_a_lost_step() -> None:
    """Check that the work log flags a step done twice, unless a kill lost it the first time."""
    work = reference_shapes.WorkLog()
    for step in ("model improve 1", "run 2", "run 2", "score 2", "score 2"):
        work.did(step)
    work.lose("run 2")
    assert work.repeated() == ["score 2"]


class _KillWorkerAfterModel(reference_shapes.ChaosSwitch):
    """A chaos switch that kills the worker at its first box command after the first model call."""

    def __init__(self) -> None:
        """Start before the model call, with the kill not used."""
        super().__init__()
        self._after_model = False
        self._used = False

    def take(self, where: str) -> str | None:
        """Arm a worker kill at the first box command after the model call, then fire it as the base switch does.

        Args:
            where: ``model`` or ``box``.

        Returns:
            The kill that fired, or ``None``.
        """
        if where == "model":
            self._after_model = True
        elif self._after_model and not self._used:
            self._used = True
            self.arm("worker")
        return super().take(where)


def _run_killed_shape(tmp_path: Path) -> reference_shapes.RunOutcome:
    """Run one shape for one round and kill its worker in the run box of the model's candidate.

    The seed is candidate 1 and the model gives candidate 2, so the kill loses the step ``run 2``.

    Args:
        tmp_path: The folder for the harness.

    Returns:
        The run's outcome.
    """
    harness = reference_shapes.Harness(tmp_path, LocalSubprocessRuntime())
    try:
        return reference_shapes.run_shape(
            harness, reference_shapes.SHAPES[0], "kill-0", rounds=1, switch=_KillWorkerAfterModel()
        )
    finally:
        harness.close()


def test_resumed_worker_does_only_the_lost_step_again(tmp_path: Path) -> None:
    """Check that a resume after a worker kill replays the committed steps and does only the lost one again.

    Args:
        tmp_path: The folder for the harness.
    """
    outcome = _run_killed_shape(tmp_path)
    assert outcome.restarts == 1
    assert outcome.work.lost == {"run 2": 1}
    assert outcome.repeated == []
    assert outcome.ok, outcome.error


def test_resume_check_finds_a_resume_that_does_committed_steps_again(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Check that the run fails when its resume calls the model and the boxes again for committed steps.

    Args:
        tmp_path: The folder for the harness.
        monkeypatch: Turns the loop's replay and its finish check off.
    """
    monkeypatch.setattr(EngineLoop, "_replayed", lambda *args, **kwargs: None)
    # With no replay, the loop's own finish check stops the run first; this test checks the harness's own count.
    monkeypatch.setattr(EngineLoop, "finish", lambda self: None)
    outcome = _run_killed_shape(tmp_path)
    assert {"model improve 1", "run 1", "score 1"} <= set(outcome.repeated)
    assert "run 2" not in outcome.repeated
    assert not outcome.ok
    assert outcome.error is not None
    assert outcome.error.startswith("a resumed worker did committed steps again")


def test_finish_stops_a_resume_that_does_not_replay(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    """Check that the host's finish call fails a resumed run whose engine did not replay its committed steps.

    Args:
        tmp_path: The folder for the harness.
        monkeypatch: Turns the loop's replay off.
    """
    monkeypatch.setattr(EngineLoop, "_replayed", lambda *args, **kwargs: None)
    outcome = _run_killed_shape(tmp_path)
    assert not outcome.ok
    assert outcome.error is not None
    assert outcome.error.startswith("StepDivergedError: The engine ended before it replayed every committed step")


def test_failed_replay_names_its_error(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    """Check that a run whose replay fails reports ``replay failed`` as its error.

    Args:
        tmp_path: The folder for the harness.
        monkeypatch: Makes the replay fail.
    """
    monkeypatch.setattr(reference_shapes, "_replays", lambda *args, **kwargs: False)
    harness = reference_shapes.Harness(tmp_path, LocalSubprocessRuntime())
    try:
        outcome = reference_shapes.run_shape(harness, reference_shapes.SHAPES[0], "replay-0", rounds=1)
    finally:
        harness.close()
    assert outcome.finished
    assert outcome.error == "replay failed"


def _small_pass_config() -> swarm_10k.SwarmConfig:
    """Return a swarm config for a pass of a few seconds: 20 agents, 5 steps each, a 0.01 s flush window.

    Returns:
        The config.
    """
    return _swarm_config(agents=20, steps_per_agent=5, flush_window_seconds=0.01)


def test_swarm_pass_gives_the_same_step_log_on_two_shards(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
    """Check that a real 1-shard pass and a real 2-shard pass store every step and the same step log.

    Args:
        monkeypatch: Puts 10 agents in each run, so the 20 agents fill one run for each shard.
        tmp_path: The folder for the shard databases and objects.
    """
    monkeypatch.delenv("LOAD_TEST_SWARM_S3_BUCKET", raising=False)
    monkeypatch.setattr(swarm_10k, "AGENTS_PER_RUN", 10)
    config = _small_pass_config()
    single = swarm_10k.run_pass(config, 1, tmp_path / "pass-1")
    sharded = swarm_10k.run_pass(config, 2, tmp_path / "pass-2")
    assert sharded["runs_per_shard"] == [1, 1]
    assert single["digest"] == sharded["digest"]
    for figures in (single, sharded):
        assert figures["commits"] == figures["steps_stored"] == 100
        assert figures["steady_seconds"] > 0
        assert (
            0 <= figures["commit_call_ms"]["p50"] <= figures["commit_call_ms"]["p99"] <= figures["step_wait_ms"]["p99"]
        )


def test_swarm_report_shows_the_commit_call_next_to_the_step_wait(monkeypatch: pytest.MonkeyPatch) -> None:
    """Check that the report gives the commit call figures and their limit next to the step wait.

    Args:
        monkeypatch: Replaces ``run_pass``.
    """
    monkeypatch.setattr(swarm_10k, "run_pass", lambda config, shards, folder: _pass_figures(shards))
    result = swarm_10k.run(_swarm_config())
    assert result.extras["max_commit_call_p99_ms"] == swarm_10k.MAX_COMMIT_CALL_P99_MS
    keys = list(result.extras["passes"][-1])
    assert keys.index("commit_call_ms") == keys.index("step_wait_ms") + 1


class _DurableCommitStore(StepLogStore):
    """A step log whose ``commit`` waits until the step is durable, as a commit with no pipeline does."""

    def commit(self, **kwargs: Any) -> str:
        """Commit the step, then wait for its flush.

        Args:
            **kwargs: The ``StepLogStore.commit`` arguments.

        Returns:
            The step id.
        """
        step_id = super().commit(**kwargs)
        self.durable(step_id, kwargs["execution_generation"], timeout=30)
        return step_id


def test_swarm_measures_a_commit_call_that_waits_for_durability(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    """Check that a shard reports a commit call that waits for the flush, so the commit call gate can fail.

    Args:
        monkeypatch: Replaces the step log with one whose ``commit`` waits for the flush.
        tmp_path: The folder for the shard's database and objects.
    """
    monkeypatch.delenv("LOAD_TEST_SWARM_S3_BUCKET", raising=False)
    monkeypatch.setattr(swarm_10k, "StepLogStore", _DurableCommitStore)
    engine = create_engine(f"sqlite:///{tmp_path / 'db.sqlite'}")
    run_id = swarm_10k.run_id_for("test", 0)
    swarm_10k._reset(engine, [run_id])
    engine.dispose()
    results: queue.Queue[dict[str, Any]] = queue.Queue()
    shard = {
        "url": f"sqlite:///{tmp_path / 'db.sqlite'}",
        "folder": str(tmp_path),
        "worker_id": "swarm-w",
        "flush_max_bytes": swarm_10k.SQLITE_FLUSH_MAX_BYTES,
    }
    config = _swarm_config(agents=2, steps_per_agent=5, flush_window_seconds=0.05)
    agents = [(run_id, "agent-00000"), (run_id, "agent-00001")]
    swarm_10k._shard_process(config, shard, agents, threading.Barrier(1), results)
    report = results.get_nowait()
    assert "error" not in report, report.get("error")
    calls = sorted(report["commit_calls"])
    assert len(calls) == 10
    # Each commit waits for its flush, which comes at least most of a flush window after the batch opens.
    assert 1000 * calls[len(calls) // 2] > swarm_10k.MAX_COMMIT_CALL_P99_MS


def test_swarm_pass_cleans_every_shard_and_keeps_the_first_error(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    """Check that a failed pack cleanup of one shard does not stop the cleanup of the next shard.

    Args:
        monkeypatch: Makes each shard's cleanup fail with its own error.
        tmp_path: The folder for the shard databases and objects.
    """
    monkeypatch.delenv("LOAD_TEST_SWARM_S3_BUCKET", raising=False)
    monkeypatch.setattr(swarm_10k, "AGENTS_PER_RUN", 10)
    cleaned: list[str] = []

    def fail(shard: swarm_10k.Shard) -> None:
        """Fail one shard's cleanup with an error that names the shard.

        Args:
            shard: The shard.

        Raises:
            OSError: Always.
        """
        cleaned.append(shard.worker_id)
        raise OSError(f"cleanup of shard {len(cleaned)} failed")

    monkeypatch.setattr(swarm_10k, "_delete_packs", fail)
    with pytest.raises(OSError, match="cleanup of shard 1 failed"):
        swarm_10k.run_pass(_small_pass_config(), 2, tmp_path)
    assert len(cleaned) == 2


@pytest.mark.parametrize(
    ("url", "env"),
    [
        ("postgresql://user@db.example.com/swarm", {}),
        ("postgresql://user@10.0.0.5:5432/swarm", {}),
        ("postgresql:///swarm", {}),
        ("postgresql://user@localhost/swarm?host=db.example.com", {}),
        ("postgresql://user@localhost/swarm?hostaddr=10.0.0.5", {}),
        ("postgresql://user@127.0.0.1/swarm?service=remote", {}),
        ("postgresql://user@localhost/swarm?dsn=hostaddr%3D192.0.2.1", {}),
        ("postgresql://user@localhost/swarm?options=-c%20x%3D1", {}),
        ("postgresql://user@localhost/swarm", {"PGHOSTADDR": "192.0.2.1"}),
        ("postgresql://user@localhost/swarm", {"PGSERVICE": "remote"}),
        ("postgresql://user@localhost/swarm", {"PGSERVICEFILE": "/etc/pg_service.conf"}),
        ("postgresql://user@localhost/swarm", {"PGHOST": "db.example.com"}),
    ],
)
def test_swarm_refuses_a_database_that_is_not_local(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path, url: str, env: dict[str, str]
) -> None:
    """Check that the swarm refuses a database URL or environment off the local host, before it opens the database.

    Args:
        monkeypatch: Makes an opened database fail the test, and sets the environment.
        tmp_path: The pass folder.
        url: The URL.
        env: The libpq environment variables to set.
    """
    for name in ("PGHOST", "PGHOSTADDR", "PGSERVICE", "PGSERVICEFILE"):
        monkeypatch.delenv(name, raising=False)
    for name, value in env.items():
        monkeypatch.setenv(name, value)
    monkeypatch.setattr(swarm_10k, "create_engine", _no_start)
    with pytest.raises(RuntimeError, match="LOAD_TEST_SWARM_DB_URL"):
        swarm_10k.run_pass(_swarm_config(db_url=url), 2, tmp_path)


def test_swarm_takes_a_local_database(monkeypatch: pytest.MonkeyPatch) -> None:
    """Check that a database URL on the local host passes the check.

    Args:
        monkeypatch: Sets the environment.
    """
    for name in ("PGHOSTADDR", "PGSERVICE", "PGSERVICEFILE"):
        monkeypatch.delenv(name, raising=False)
    monkeypatch.setenv("PGHOST", "localhost")
    for url in (
        None,
        "postgresql://user@localhost:5432/swarm",
        "postgresql://user@127.0.0.1/swarm",
        "postgresql://user@localhost/swarm?sslmode=disable&connect_timeout=5&application_name=swarm",
    ):
        swarm_10k._check_db_url(url)


class _FakeServer:
    """A stand-in Postgres engine that answers ``inet_server_addr()`` with a fixed address."""

    def __init__(self, address: str | None) -> None:
        """Keep the address.

        Args:
            address: The server address, or ``None`` for a Unix socket.
        """
        self.address = address
        self.disposed = False

    def connect(self) -> Any:
        """Return a connection whose one query gives the address.

        Returns:
            The connection, as a context manager.
        """
        result = SimpleNamespace(scalar=lambda: self.address)
        connection = SimpleNamespace(execute=lambda statement: result)
        return nullcontext(connection)

    def dispose(self) -> None:
        """Record that the pass closed the engine's pool."""
        self.disposed = True


@pytest.mark.parametrize("address", ["192.0.2.1", "10.0.0.5/32", "2001:db8::1"])
def test_swarm_refuses_a_server_off_loopback(monkeypatch: pytest.MonkeyPatch, tmp_path: Path, address: str) -> None:
    """Check that the swarm refuses a server off loopback before it resets any row, and closes the engine's pool.

    Args:
        monkeypatch: Replaces the engine and makes a reset fail the test.
        tmp_path: The pass folder.
        address: The server address.
    """
    for name in ("PGHOST", "PGHOSTADDR", "PGSERVICE", "PGSERVICEFILE"):
        monkeypatch.delenv(name, raising=False)
    server = _FakeServer(address)
    monkeypatch.setattr(swarm_10k, "create_engine", lambda url, connect_args: server)
    monkeypatch.setattr(swarm_10k, "_reset", _no_start)
    with pytest.raises(RuntimeError, match="not loopback"):
        swarm_10k.run_pass(_swarm_config(db_url="postgresql://user@localhost/swarm"), 2, tmp_path)
    assert server.disposed


@pytest.mark.parametrize("address", [None, "127.0.0.1", "::1"])
def test_swarm_takes_a_loopback_server(address: str | None) -> None:
    """Check that a server on loopback, or on a Unix socket, passes the address check.

    Args:
        address: The server address, or ``None`` for a Unix socket.
    """
    swarm_10k._check_server_addr(_FakeServer(address))


def test_swarm_box_caps_are_not_gated(monkeypatch: pytest.MonkeyPatch) -> None:
    """Check that the swarm reports its box load with the mark that the box caps are not gated.

    Args:
        monkeypatch: Replaces ``run_pass`` and sets the backend.
    """
    monkeypatch.setattr(swarm_10k, "run_pass", lambda config, shards, folder: _pass_figures(shards))
    result = swarm_10k.run(_target_backend(monkeypatch, _swarm_config(box_seconds=1_000_000.0)))
    assert result.extras["box_caps_gated"] is False
    assert result.extras["box_load_target"]["fresh_box_per_step"]["concurrency_within_cap"] is False
    assert result.slo_passed


def _run_one(tmp_path: Path, shape: reference_shapes.Shape, switch: Any = None) -> reference_shapes.RunOutcome:
    """Run one shape for one round on local subprocess boxes.

    Args:
        tmp_path: The folder for the harness.
        shape: The shape.
        switch: The run's chaos switch, or ``None``.

    Returns:
        The run's outcome.
    """
    harness = reference_shapes.Harness(tmp_path, LocalSubprocessRuntime())
    try:
        return reference_shapes.run_shape(harness, shape, "one-0", rounds=1, switch=switch)
    finally:
        harness.close()


def test_run_whose_every_step_fails_is_not_ok(tmp_path: Path) -> None:
    """Check that a run whose step command always fails does not pass, although it finishes and replays.

    Args:
        tmp_path: The folder for the harness.
    """
    outcome = _run_one(tmp_path, replace(reference_shapes.SHAPES[0], work_script="exit 3\n"))
    assert outcome.finished
    assert outcome.result is not None
    assert outcome.result[1] is None
    assert not outcome.correct
    assert not outcome.ok


def test_run_with_a_wrong_result_is_not_ok(tmp_path: Path) -> None:
    """Check that a run whose score is not the shape's own score of its best candidate does not pass.

    Args:
        tmp_path: The folder for the harness.
    """
    outcome = _run_one(tmp_path, replace(reference_shapes.SHAPES[1], score=lambda candidate: -1.0))
    assert not outcome.ok
    assert outcome.error is not None
    assert outcome.error.startswith("wrong result")


def test_expected_result_matches_a_clean_run(tmp_path: Path) -> None:
    """Check that a run with no chaos gives the result that the shape's score formula gives.

    Args:
        tmp_path: The folder for the harness.
    """
    shape = reference_shapes.SHAPES[1]
    outcome = _run_one(tmp_path, shape)
    assert outcome.ok, outcome.error
    assert outcome.result == reference_shapes.expected_result(shape, 1) == ("2", 133.333, 2)


def test_gpu_shape_checks_each_placement(tmp_path: Path) -> None:
    """Check that the GPU shape opens each box through a checked ``gpu:<type>`` placement.

    Args:
        tmp_path: The folder for the harness.
    """
    shape = reference_shapes.SHAPES[3]
    outcome = _run_one(tmp_path, shape)
    row = reference_shapes.outcome_report(shape, outcome, 1.0)
    assert outcome.meter.opened > 0
    assert row["gpu_placements"] == {"gpu:A10G": outcome.meter.opened}
    assert row["gpu_placement_checked"] is True
    assert row["ok"] is True


@pytest.mark.parametrize(("opened", "placed"), [(0, 0), (4, 3)])
def test_gpu_check_fails_with_no_box_or_an_unchecked_box(opened: int, placed: int) -> None:
    """Check that the GPU check fails with no box at all, and with a box that skipped the placement check.

    Args:
        opened: The boxes that the run opened.
        placed: The boxes whose placement was checked.
    """
    shape = reference_shapes.SHAPES[3]
    outcome = reference_shapes.RunOutcome("gpu-0", shape.name)
    outcome.meter.opened = opened
    outcome.meter.placements["gpu:A10G"] = placed
    row = reference_shapes.outcome_report(shape, outcome, 1.0)
    assert row["gpu_placement_checked"] is False
    assert row["ok"] is False


def _passing_outcome(shape: reference_shapes.Shape) -> reference_shapes.RunOutcome:
    """Return an outcome that passes every check of :attr:`RunOutcome.ok`.

    Args:
        shape: The shape.

    Returns:
        The outcome, with one opened box.
    """
    outcome = reference_shapes.RunOutcome(
        "made-0",
        shape.name,
        finished=True,
        result=reference_shapes.expected_result(shape, 1),
        gapless=True,
        replayed=True,
        correct=True,
    )
    outcome.meter.opened = 1
    return outcome


def test_gpu_check_fails_a_run_that_is_ok_otherwise() -> None:
    """Check that an unchecked GPU placement fails the report row of a run that passes every other check."""
    shape = reference_shapes.SHAPES[3]
    outcome = _passing_outcome(shape)
    outcome.meter.opened = 4
    outcome.meter.placements["gpu:A10G"] = 3
    assert outcome.ok
    row = reference_shapes.outcome_report(shape, outcome, 1.0)
    assert row["gpu_placement_checked"] is False
    assert row["ok"] is False


def test_run_with_a_seq_gap_is_not_ok() -> None:
    """Check that a step log with a seq gap fails the run, also when every other check passes."""
    assert not reference_shapes._gapless([("a", 0, "model", 1), ("a", 2, "command", 1)])
    outcome = _passing_outcome(reference_shapes.SHAPES[0])
    assert outcome.ok
    outcome.gapless = False
    assert not outcome.ok


def test_run_with_no_box_or_no_replay_is_not_ok() -> None:
    """Check that a run that opened no box, or whose replay failed, is not ok when every other check passes."""
    outcome = _passing_outcome(reference_shapes.SHAPES[0])
    assert outcome.ok
    outcome.replayed = False
    assert not outcome.ok
    outcome = _passing_outcome(reference_shapes.SHAPES[0])
    outcome.meter.opened = 0
    assert not outcome.ok


def test_replay_with_another_result_fails(tmp_path: Path) -> None:
    """Check that a clean replay fails when its result is not the live run's result.

    Args:
        tmp_path: The folder for the harness.
    """
    shape = reference_shapes.SHAPES[0]
    harness = reference_shapes.Harness(tmp_path, LocalSubprocessRuntime())
    try:
        outcome = reference_shapes.run_shape(harness, shape, "one-0", rounds=1)
        assert outcome.ok, outcome.error
        assert outcome.result is not None
        best, score, evals = outcome.result
        assert not reference_shapes._replays(harness, shape, "one-0", 1, (best, score, evals + 1))
        assert reference_shapes._replays(harness, shape, "one-0", 1, outcome.result)
    finally:
        harness.close()


def test_run_that_repeats_a_committed_step_is_not_ok() -> None:
    """Check that a resumed worker that did a committed step again fails the run, also when the replay works."""
    outcome = _passing_outcome(reference_shapes.SHAPES[0])
    outcome.repeated = ["run 2"]
    assert outcome.replayed
    assert not outcome.ok


def test_result_that_is_not_the_expected_result_is_not_correct() -> None:
    """Check that a result whose score is its candidate's own score, but whose eval count is wrong, is not correct."""
    shape = reference_shapes.SHAPES[1]
    best, score, evals = reference_shapes.expected_result(shape, 1)
    assert score == shape.score(int(best))
    assert reference_shapes._correct(shape, 1, (best, score, evals))
    assert not reference_shapes._correct(shape, 1, (best, score, evals + 1))


@pytest.mark.parametrize("leak", ["box", "pack"])
def test_replay_that_opens_a_box_or_stores_a_pack_fails(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path, leak: str
) -> None:
    """Check that a replay with the same result fails when it opens a box or stores new data.

    Args:
        monkeypatch: Makes the replay's meter count a box, or its footprint grow.
        tmp_path: The folder for the harness.
        leak: ``box`` or ``pack``.
    """
    shape = reference_shapes.SHAPES[0]
    harness = reference_shapes.Harness(tmp_path, LocalSubprocessRuntime())
    try:
        outcome = reference_shapes.run_shape(harness, shape, "one-0", rounds=1)
        assert outcome.ok, outcome.error
        if leak == "box":

            class _OpenedMeter(reference_shapes.BoxMeter):
                """A meter that counts one box before the replay starts."""

                def __init__(self) -> None:
                    """Start with one opened box."""
                    super().__init__()
                    self.opened = 1

            monkeypatch.setattr(reference_shapes, "BoxMeter", _OpenedMeter)
        else:
            footprints = iter([(1, 1, 1, 1), (1, 2, 1, 2)])
            monkeypatch.setattr(harness, "footprint", lambda run_id: next(footprints))
        assert not reference_shapes._replays(harness, shape, "one-0", 1, outcome.result)
    finally:
        harness.close()


class _KillFirstBox(reference_shapes.ChaosSwitch):
    """A chaos switch that kills the run's first box."""

    def __init__(self) -> None:
        """Start with the box kill armed."""
        super().__init__()
        self.arm("box")


def test_killed_box_commits_a_failure_that_replays(tmp_path: Path) -> None:
    """Check that a killed box gives a committed box failure, and that the run still passes and replays.

    The kill fails the seed's run box, so the seed has no score and the model's candidate 2 wins.

    Args:
        tmp_path: The folder for the harness.
    """
    shape = reference_shapes.SHAPES[0]
    switch = _KillFirstBox()
    outcome = _run_one(tmp_path, shape, switch)
    assert switch.fired == {"box": 1}
    assert outcome.restarts == 0
    assert outcome.result == ("2", shape.score(2), 2)
    assert switch.failed_candidates() == {1}
    assert outcome.replayed
    assert outcome.ok, outcome.error


def test_box_killed_run_must_give_the_expected_result() -> None:
    """Check that a run whose box a kill failed must still give the expected result with that candidate failed."""
    shape = reference_shapes.SHAPES[0]
    failed = frozenset({1})
    assert reference_shapes.expected_result(shape, 1, failed) == ("2", shape.score(2), 2)
    assert reference_shapes._correct(shape, 1, ("2", shape.score(2), 2), failed=failed)
    # The score agrees with the candidate, but the seed's box failed, so the seed cannot win.
    assert not reference_shapes._correct(shape, 1, ("1", shape.score(1), 2), failed=failed)
    assert not reference_shapes._correct(shape, 1, ("2", shape.score(2), 3), failed=failed)


def test_redone_box_step_clears_its_box_kill() -> None:
    """Check that a box step done again after a lost kill counts by its last attempt."""
    switch = reference_shapes.ChaosSwitch()
    switch.box_attempt("run 2", True)
    switch.box_attempt("score 3", True)
    assert switch.failed_candidates() == {2, 3}
    switch.box_attempt("run 2", False)
    assert switch.failed_candidates() == {3}


class _KillAfterModelCommit(reference_shapes.ChaosSwitch):
    """A chaos switch that kills the worker right after the first model step commits."""

    def __init__(self) -> None:
        """Start before the model call, with the kill not used."""
        super().__init__()
        self._after_model = False
        self._used = False

    def take(self, where: str) -> str | None:
        """Arm a commit kill at the first commit after the model call, then fire it as the base switch does.

        Args:
            where: ``model``, ``box`` or ``commit``.

        Returns:
            The kill that fired, or ``None``.
        """
        if where == "model":
            self._after_model = True
        elif where == "commit" and self._after_model and not self._used:
            self._used = True
            self.arm("commit")
        return super().take(where)


def test_commit_kill_loses_the_step_before_the_flush(tmp_path: Path) -> None:
    """Check that a kill after a commit and before the flush loses the step, and the resume does it again.

    Args:
        tmp_path: The folder for the harness.
    """
    switch = _KillAfterModelCommit()
    outcome = _run_one(tmp_path, reference_shapes.SHAPES[0], switch)
    assert switch.fired == {"commit": 1}
    assert outcome.restarts == 1
    assert outcome.cuts_lost == 1
    assert outcome.work.lost == {"model improve 1": 1}
    assert outcome.work.done["model improve 1"] == 2
    assert outcome.repeated == []
    assert outcome.ok, outcome.error


def test_commit_kill_after_the_flush_does_not_excuse_a_redo(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    """Check that a cut step stored in its own generation takes back its lost try, so its redo fails the run.

    The flusher can store the step before the fence moves. Then the kill lost nothing, and a resume that does
    the step again did committed work again.

    Args:
        tmp_path: The folder for the harness.
        monkeypatch: Makes the harness report each cut step as stored in the generation of its commit.
    """
    switch = _KillAfterModelCommit()
    monkeypatch.setattr(
        reference_shapes.Harness,
        "stored_generation",
        lambda self, step_id: next(generation for cut_id, generation, _ in switch.cuts if cut_id == step_id),
    )
    outcome = _run_one(tmp_path, reference_shapes.SHAPES[0], switch)
    assert switch.fired == {"commit": 1}
    assert outcome.cuts_lost == 0
    assert outcome.work.lost == {"model improve 1": 0}
    assert outcome.repeated == ["model improve 1"]
    assert not outcome.ok


def test_reference_shapes_fails_a_shape_that_is_not_ok(monkeypatch: pytest.MonkeyPatch) -> None:
    """Check that the reference run names each shape whose run is not ok, and only that shape.

    Args:
        monkeypatch: Replaces ``run_shape`` with made-up outcomes and keeps the boxes local.
    """
    broken = reference_shapes.SHAPES[1]

    def made_run(
        harness: Any, shape: reference_shapes.Shape, run_id: str, *, rounds: int
    ) -> reference_shapes.RunOutcome:
        """Return a passing outcome, or a not-ok one for the broken shape.

        Args:
            harness: Not used.
            shape: The shape.
            run_id: Not used.
            rounds: Not used.

        Returns:
            The outcome.
        """
        outcome = _passing_outcome(shape)
        if shape.gpu:
            outcome.meter.placements["gpu:A10G"] = 1
        if shape is broken:
            outcome.replayed = False
        return outcome

    monkeypatch.delenv("LOAD_TEST_BOX_IMAGE", raising=False)
    monkeypatch.setattr(reference_shapes, "run_shape", made_run)
    result = reference_shapes.run(reference_shapes.ReferenceShapesConfig(rounds=1, model_wait_seconds=1.0))
    assert result.slo_violations == [f"{broken.name} did not finish correct and replayable"]
    assert not result.slo_passed


def test_chaos_gate_fails_a_run_that_is_not_ok(monkeypatch: pytest.MonkeyPatch) -> None:
    """Check that the chaos gate names a run that is not ok, also when every kill kind fired and a cut was lost.

    Args:
        monkeypatch: Runs one made-up shape run that fires each kill kind, and stops the chaos job.
    """
    shape = reference_shapes.SHAPES[0]

    def broken_run(
        harness: Any, made_shape: reference_shapes.Shape, run_id: str, *, rounds: int, switch: Any
    ) -> reference_shapes.RunOutcome:
        """Fire each kill kind on the switch and return an outcome whose replay failed.

        Args:
            harness: Not used.
            made_shape: The shape.
            run_id: Not used.
            rounds: Not used.
            switch: The run's chaos switch.

        Returns:
            The outcome.
        """
        switch.fired.update(dict.fromkeys(reference_shapes.KILL_KINDS, 1))
        outcome = _passing_outcome(made_shape)
        outcome.cuts_lost = 1
        outcome.replayed = False
        outcome.error = "replay failed"
        return outcome

    monkeypatch.setattr(chaos_gate, "SHAPES", (shape,))
    monkeypatch.setattr(chaos_gate, "run_shape", broken_run)
    monkeypatch.setattr(chaos_gate.ChaosJob, "run", lambda self: self._halt.wait())
    config = chaos_gate.ChaosGateConfig(
        runs_per_shape=1, rounds=1, workers=1, kill_interval_seconds=0.1, seed=1, model_wait_seconds=1.0
    )
    result = chaos_gate.run(config)
    assert result.extras["runs_ok"] == 0
    assert result.slo_violations == [f"made-0 ({shape.name}) did not finish correct and replayable: replay failed"]
    assert not result.slo_passed


def test_chaos_gate_fails_when_a_kill_kind_never_fires(monkeypatch: pytest.MonkeyPatch) -> None:
    """Check that the chaos gate fails when its job fires no kill, also when every run passes.

    Args:
        monkeypatch: Runs one shape and stops the chaos job from arming a kill.
    """
    monkeypatch.setattr(chaos_gate, "SHAPES", reference_shapes.SHAPES[:1])
    monkeypatch.setattr(chaos_gate.ChaosJob, "run", lambda self: self._halt.wait())
    config = chaos_gate.ChaosGateConfig(
        runs_per_shape=1, rounds=1, workers=1, kill_interval_seconds=0.1, seed=1, model_wait_seconds=1.0
    )
    result = chaos_gate.run(config)
    assert result.extras["runs_ok"] == 1
    assert result.slo_violations == ["the chaos job did not fire every kill kind: {'box': 0, 'worker': 0, 'commit': 0}"]
    assert not result.slo_passed


def test_chaos_gate_fails_when_the_fence_drops_no_step(monkeypatch: pytest.MonkeyPatch) -> None:
    """Check that the chaos gate fails when every kill kind fires but no commit kill lost a step.

    Args:
        monkeypatch: Runs one made-up shape run that fires each kill kind, and stops the chaos job.
    """
    shape = reference_shapes.SHAPES[0]

    def fire_all(
        harness: Any, made_shape: reference_shapes.Shape, run_id: str, *, rounds: int, switch: Any
    ) -> reference_shapes.RunOutcome:
        """Fire each kill kind on the switch and return a passing outcome with no lost cut.

        Args:
            harness: Not used.
            made_shape: The shape.
            run_id: Not used.
            rounds: Not used.
            switch: The run's chaos switch.

        Returns:
            The outcome.
        """
        switch.fired.update(dict.fromkeys(reference_shapes.KILL_KINDS, 1))
        return _passing_outcome(made_shape)

    monkeypatch.setattr(chaos_gate, "SHAPES", (shape,))
    monkeypatch.setattr(chaos_gate, "run_shape", fire_all)
    monkeypatch.setattr(chaos_gate.ChaosJob, "run", lambda self: self._halt.wait())
    config = chaos_gate.ChaosGateConfig(
        runs_per_shape=1, rounds=1, workers=1, kill_interval_seconds=0.1, seed=1, model_wait_seconds=1.0
    )
    result = chaos_gate.run(config)
    assert result.extras["runs_ok"] == 1
    assert result.slo_violations == ["no commit kill landed before the flush, so the fence dropped no step"]
    assert not result.slo_passed


class _NoProcess:
    """A shard process that never starts: the test hands its report in directly."""

    def __init__(self, **kwargs: Any) -> None:
        """Take the process arguments and do nothing.

        Args:
            **kwargs: Not used.
        """

    def start(self) -> None:
        """Start nothing."""

    def join(self, timeout: float | None = None) -> None:
        """Wait for nothing.

        Args:
            timeout: Not used.
        """

    def is_alive(self) -> bool:
        """Return that the process is gone.

        Returns:
            ``False``.
        """
        return False


def test_swarm_pass_rates_survive_a_pass_with_no_time_after_the_first_round(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    """Check that a pass whose last step is durable when the first round ends gives 0 rates, not a crash.

    Args:
        monkeypatch: Replaces the shard processes with one made-up report.
        tmp_path: The pass folder.
    """
    context = SimpleNamespace(Barrier=lambda count: None, Queue=lambda: None, Process=_NoProcess)
    monkeypatch.setattr(swarm_10k, "multiprocessing", SimpleNamespace(get_context=lambda method: context))
    report = {
        "worker_id": "swarm-w",
        "started": 0.5,
        "waits": [0.1],
        "lates": [0.0],
        "commit_calls": [0.0],
        "times": [1.0],
        "first_round_durable": 1.0,
        "first_agent_done": 1.0,
        "boxes_opened": 0,
        "boxes_open_max": 0,
    }
    monkeypatch.setattr(swarm_10k, "collect_reports", lambda *args: [report])
    monkeypatch.setattr(swarm_10k, "_digest", lambda shards: ("digest", 1))
    figures = swarm_10k.run_pass(_small_pass_config(), 1, tmp_path)
    assert figures["commits_per_second"] == 0.0
    assert figures["commits_per_second_with_drain"] == 0.0


def _named_sqlite(
    path: Path,
    name: str,
    lock_free: bool = True,
    unlocks: list[int] | None = None,
    fails: str | None = None,
) -> Engine:
    """Return a SQLite engine that answers ``current_database()`` and the advisory lock calls, as Postgres does.

    Args:
        path: The database file.
        name: The database name.
        lock_free: What ``pg_try_advisory_lock`` answers: ``False`` when another pass holds the lock.
        unlocks: Gets the key of each ``pg_advisory_unlock`` call, or ``None``.
        fails: The name of the lock function that raises, or ``None``.

    Returns:
        The engine.
    """
    engine = create_engine(f"sqlite:///{path}")

    def unlock(key: int) -> bool:
        """Record one unlock.

        Args:
            key: The lock key.

        Returns:
            ``True``, as Postgres does for a held lock.
        """
        if unlocks is not None:
            unlocks.append(key)
        if fails == "pg_advisory_unlock":
            raise OSError("unlock failed")
        return True

    def lock(key: int) -> bool:
        """Answer one lock call.

        Args:
            key: Not used.

        Returns:
            ``lock_free``.

        Raises:
            OSError: When ``fails`` names this function.
        """
        if fails == "pg_try_advisory_lock":
            raise OSError("lock failed")
        return lock_free

    def on_connect(connection: Any, record: Any) -> None:
        """Add the Postgres functions that the swarm calls to a new SQLite connection.

        Args:
            connection: The DBAPI connection.
            record: Not used.
        """
        connection.create_function("current_database", 0, lambda: name)
        connection.create_function("pg_try_advisory_lock", 1, lock)
        connection.create_function("pg_advisory_unlock", 1, unlock)

    event.listen(engine, "connect", on_connect)
    return engine


def _postgres_pass(monkeypatch: pytest.MonkeyPatch, engine: Engine) -> None:
    """Send a swarm pass with a Postgres URL to a SQLite engine, with no shard process.

    Args:
        monkeypatch: Replaces the engine, the server address check and the shard processes.
        engine: The engine that the pass gets for the Postgres URL.
    """
    for name in ("PGHOST", "PGHOSTADDR", "PGSERVICE", "PGSERVICEFILE"):
        monkeypatch.delenv(name, raising=False)
    monkeypatch.delenv("LOAD_TEST_SWARM_S3_BUCKET", raising=False)
    monkeypatch.setattr(swarm_10k, "create_engine", lambda url, connect_args: engine)
    # SQLite has no inet_server_addr(); the address check has its own tests.
    monkeypatch.setattr(swarm_10k, "_check_server_addr", lambda engine: None)
    context = SimpleNamespace(Barrier=lambda count: None, Queue=lambda: None, Process=_NoProcess)
    monkeypatch.setattr(swarm_10k, "multiprocessing", SimpleNamespace(get_context=lambda method: context))
    report = {
        "worker_id": "swarm-w",
        "started": 0.5,
        "waits": [0.1],
        "lates": [0.0],
        "commit_calls": [0.0],
        "times": [1.0],
        "first_round_durable": 1.0,
        "first_agent_done": 1.0,
        "boxes_opened": 0,
        "boxes_open_max": 0,
    }
    monkeypatch.setattr(swarm_10k, "collect_reports", lambda *args: [report])


@pytest.mark.parametrize("name", ["skynet", "postgres", "swarm", "my_loadtest"])
def test_swarm_refuses_a_database_without_the_throwaway_name(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path, name: str
) -> None:
    """Check that the swarm refuses a database with no throwaway name before any reset, and closes the engine's pool.

    Args:
        monkeypatch: Replaces the engine and makes a reset fail the test.
        tmp_path: The pass folder.
        name: The database name.
    """
    engine = _named_sqlite(tmp_path / "db.sqlite", name)
    _postgres_pass(monkeypatch, engine)
    monkeypatch.setattr(swarm_10k, "_reset", _no_start)
    with pytest.raises(RuntimeError, match="must start with 'loadtest'"):
        swarm_10k.run_pass(_swarm_config(db_url="postgresql://user@localhost/skynet"), 2, tmp_path / "pass")
    # The check opened a connection; only a dispose empties the pool again.
    assert engine.pool.checkedin() == 0


def test_swarm_refuses_a_database_with_another_job(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
    """Check that the swarm refuses a throwaway-named database that holds a job of another run, before any reset.

    Args:
        monkeypatch: Replaces the engine and makes a reset fail the test.
        tmp_path: The pass folder.
    """
    engine = _named_sqlite(tmp_path / "db.sqlite", "loadtest_swarm")
    swarm_10k._reset(engine, ["swarm-old"])
    with Session(engine) as session:
        session.add(JobModel(optimization_id="opt-real", username="someone", status="running"))
        session.commit()
    _postgres_pass(monkeypatch, engine)
    monkeypatch.setattr(swarm_10k, "_reset", _no_start)
    with pytest.raises(RuntimeError, match="'opt-real', which is not a swarm run"):
        swarm_10k.run_pass(_swarm_config(db_url="postgresql://user@localhost/loadtest_swarm"), 2, tmp_path / "pass")


def test_swarm_takes_a_throwaway_database(tmp_path: Path) -> None:
    """Check that a throwaway-named database with no jobs table, or with only swarm jobs, passes the check.

    Args:
        tmp_path: The folder for the database.
    """
    engine = _named_sqlite(tmp_path / "db.sqlite", "loadtest_swarm")
    swarm_10k._check_throwaway(engine)
    swarm_10k._reset(engine, ["swarm-old"])
    swarm_10k._check_throwaway(engine)


def _seed_rows_after_reset(monkeypatch: pytest.MonkeyPatch, engine: Engine, worker_id: str) -> None:
    """Make the reset also put in one swarm row in each step-log table, and one row of another worker.

    The rows go in after the reset's own delete, as the shard processes' rows do, so only the cleanup removes them.

    Args:
        monkeypatch: Wraps ``_reset``.
        engine: The database.
        worker_id: The worker id of the pass's first shard.
    """
    reset = swarm_10k._reset

    def seeded(reset_engine: Engine, runs: list[str]) -> None:
        """Reset the database, then put in the rows.

        Args:
            reset_engine: The database to reset.
            runs: The job ids.
        """
        reset(reset_engine, runs)
        pack = {"pack_key": f"packs/{worker_id}/0", "pack_offset": 0, "pack_length": 1}
        with Session(engine) as session:
            session.add_all(
                [
                    AgentStepModel(
                        step_id=f"{runs[0]}:agent-00000:1",
                        run_id=runs[0],
                        agent_id="agent-00000",
                        seq=1,
                        kind="command",
                        execution_generation=1,
                        grant_list_id="grants",
                        **pack,
                    ),
                    TreeObjectModel(username=runs[0], object_id="tree", byte_size=1, **pack),
                    StoragePackModel(pack_key=f"packs/{worker_id}/0", worker_id=worker_id, byte_size=1),
                    TreeObjectModel(username="someone", object_id="tree", byte_size=1, **pack),
                    StoragePackModel(pack_key="packs/other/0", worker_id="other", byte_size=1),
                ]
            )
            session.commit()

    monkeypatch.setattr(swarm_10k, "_reset", seeded)


def _rows(engine: Engine) -> dict[str, list[str]]:
    """Return the key of each row in the step-log tables.

    Args:
        engine: The database.

    Returns:
        The keys, by table.
    """
    with Session(engine) as session:
        return {
            "jobs": list(session.execute(select(JobModel.optimization_id)).scalars()),
            "agent_steps": list(session.execute(select(AgentStepModel.step_id)).scalars()),
            "tree_objects": list(session.execute(select(TreeObjectModel.username)).scalars()),
            "storage_packs": list(session.execute(select(StoragePackModel.worker_id)).scalars()),
        }


_OTHER_ROWS = {"jobs": [], "agent_steps": [], "tree_objects": ["someone"], "storage_packs": ["other"]}


def test_swarm_postgres_pass_leases_its_jobs_and_deletes_its_rows(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    """Check that a shared-database pass keeps a live lease on its jobs and deletes only its own rows when it ends.

    A local worker's orphan sweep takes a running job with no lease, or a lapsed one.

    Args:
        monkeypatch: Replaces the engine, the shard processes and the step log read.
        tmp_path: The pass folder.
    """
    engine = _named_sqlite(tmp_path / "db.sqlite", "loadtest_swarm")
    config = replace(_small_pass_config(), db_url="postgresql://user@localhost/loadtest_swarm")
    _postgres_pass(monkeypatch, engine)
    _seed_rows_after_reset(monkeypatch, engine, swarm_10k.worker_id_for(config.stamp, 1, 0))
    leases: list[datetime | None] = []

    def digest(shards: list[swarm_10k.Shard]) -> tuple[str, int]:
        """Record the job leases during the pass.

        Args:
            shards: The shards.

        Returns:
            A made-up digest and step count.
        """
        with Session(engine) as session:
            leases.extend(session.execute(select(JobModel.lease_expires_at)).scalars())
        return "digest", 1

    monkeypatch.setattr(swarm_10k, "_digest", digest)
    swarm_10k.run_pass(config, 1, tmp_path / "pass")
    assert leases
    assert all(lease is not None and lease.replace(tzinfo=UTC) > datetime.now(UTC) for lease in leases)
    assert _rows(engine) == _OTHER_ROWS


def test_swarm_cleanup_keeps_a_job_of_another_run(tmp_path: Path) -> None:
    """Check that the cleanup deletes only swarm jobs, so a job that another process adds during a pass stays.

    Args:
        tmp_path: The database folder.
    """
    engine = _named_sqlite(tmp_path / "db.sqlite", "loadtest_swarm")
    swarm_10k._reset(engine, ["swarm-0"])
    with Session(engine) as session:
        session.add(JobModel(optimization_id="opt-real", username="someone", status="running"))
        session.commit()
    with Session(engine) as session:
        swarm_10k._delete_swarm_rows(session, ["swarm-0"], [])
        session.commit()
    assert _rows(engine)["jobs"] == ["opt-real"]
    engine.dispose()


def _object_store_fails(folder: Path) -> tuple[Any, Any]:
    """Fail as a bad object store setting does.

    Args:
        folder: Not used.

    Raises:
        RuntimeError: Always.
    """
    raise RuntimeError("object store failed")


@pytest.mark.parametrize("failure", ["shard report", "object store"])
def test_swarm_postgres_pass_that_fails_deletes_its_rows(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path, failure: str
) -> None:
    """Check that a failed shared-database pass deletes its own rows and keeps the rows of another worker.

    The object store failure comes after the reset and before the first shard joins the pass.

    Args:
        monkeypatch: Replaces the engine, the shard processes, and the report or the object store.
        tmp_path: The pass folder.
        failure: The step that fails.
    """
    engine = _named_sqlite(tmp_path / "db.sqlite", "loadtest_swarm")
    config = replace(_small_pass_config(), db_url="postgresql://user@localhost/loadtest_swarm")
    _postgres_pass(monkeypatch, engine)
    _seed_rows_after_reset(monkeypatch, engine, swarm_10k.worker_id_for(config.stamp, 2, 0))
    if failure == "shard report":
        report = {"worker_id": "swarm-w", "error": "shard broke"}
        monkeypatch.setattr(swarm_10k, "collect_reports", lambda *args: [report])
    else:
        monkeypatch.setattr(swarm_10k, "_object_store", _object_store_fails)
    with pytest.raises(RuntimeError, match=r"shard broke|object store failed"):
        swarm_10k.run_pass(config, 2, tmp_path / "pass")
    assert _rows(engine) == _OTHER_ROWS


def test_swarm_pass_that_fails_closes_the_pool_of_a_shard_not_in_the_pass(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    """Check that an engine whose shard fails before it joins the pass gets its pool closed.

    Args:
        monkeypatch: Replaces the engine and the shard processes, and makes the object store fail.
        tmp_path: The pass folder.
    """
    engine = _named_sqlite(tmp_path / "db.sqlite", "loadtest_swarm")
    _postgres_pass(monkeypatch, engine)
    monkeypatch.setattr(swarm_10k, "_object_store", _object_store_fails)
    config = replace(_small_pass_config(), db_url="postgresql://user@localhost/loadtest_swarm")
    with pytest.raises(RuntimeError, match="object store failed"):
        swarm_10k.run_pass(config, 1, tmp_path / "pass")
    # The checks, the reset and the row cleanup opened connections; only a dispose empties the pool again.
    assert engine.pool.checkedin() == 0


def test_swarm_pass_that_fails_names_a_failed_cleanup(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
    """Check that a failed pass whose cleanup also fails raises the pass error with a note on the cleanup error.

    Args:
        monkeypatch: Replaces the engine and the shard processes, fails the shard, and fails the row cleanup.
        tmp_path: The pass folder.
    """
    engine = _named_sqlite(tmp_path / "db.sqlite", "loadtest_swarm")
    _postgres_pass(monkeypatch, engine)
    report = {"worker_id": "swarm-w", "error": "shard broke"}
    monkeypatch.setattr(swarm_10k, "collect_reports", lambda *args: [report])

    def fail(session: Session, *ids: list[str]) -> None:
        """Fail the row cleanup.

        Args:
            session: Not used.
            *ids: Not used.

        Raises:
            OSError: Always.
        """
        raise OSError("row cleanup failed")

    reset = swarm_10k._reset

    def reset_then_fail_cleanup(reset_engine: Engine, runs: list[str]) -> None:
        """Reset the database, then make the later row cleanup fail.

        Args:
            reset_engine: The database to reset.
            runs: The job ids.
        """
        reset(reset_engine, runs)
        monkeypatch.setattr(swarm_10k, "_delete_swarm_rows", fail)

    monkeypatch.setattr(swarm_10k, "_reset", reset_then_fail_cleanup)
    config = replace(_small_pass_config(), db_url="postgresql://user@localhost/loadtest_swarm")
    with pytest.raises(RuntimeError, match="shard broke") as raised:
        swarm_10k.run_pass(config, 1, tmp_path / "pass")
    assert any("row cleanup failed" in note and "can be left" in note for note in raised.value.__notes__)


def test_swarm_refuses_a_remote_s3_endpoint_before_any_reset(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
    """Check that a bucket with an endpoint off the local host stops the pass before the database gets a row.

    Args:
        monkeypatch: Replaces the engine, sets the S3 settings, and makes a reset fail the test.
        tmp_path: The pass folder.
    """
    _postgres_pass(monkeypatch, _named_sqlite(tmp_path / "db.sqlite", "loadtest_swarm"))
    monkeypatch.setenv("LOAD_TEST_SWARM_S3_BUCKET", "swarm")
    monkeypatch.setenv("LOAD_TEST_SWARM_S3_ENDPOINT", "https://s3.example.com")
    monkeypatch.setattr(swarm_10k, "_reset", _no_start)
    config = replace(_small_pass_config(), db_url="postgresql://user@localhost/loadtest_swarm")
    with pytest.raises(RuntimeError, match="needs LOAD_TEST_SWARM_S3_ENDPOINT"):
        swarm_10k.run_pass(config, 1, tmp_path / "pass")


def _other_pass_rows(engine: Engine, config: swarm_10k.SwarmConfig) -> None:
    """Put in the job, step, tree object and pack rows of another swarm invocation, as a concurrent pass leaves them.

    Args:
        engine: The database.
        config: The other invocation's config; its stamp names the rows.
    """
    run_id = swarm_10k.run_id_for(config.stamp, 0)
    worker_id = swarm_10k.worker_id_for(config.stamp, 1, 0)
    pack = {"pack_key": f"packs/{worker_id}/0", "pack_offset": 0, "pack_length": 1}
    swarm_10k._reset(engine, [run_id])
    with Session(engine) as session:
        session.add_all(
            [
                AgentStepModel(
                    step_id=f"{run_id}:agent-00000:1",
                    run_id=run_id,
                    agent_id="agent-00000",
                    seq=1,
                    kind="command",
                    execution_generation=1,
                    grant_list_id="grants",
                    **pack,
                ),
                TreeObjectModel(username=run_id, object_id="tree", byte_size=1, **pack),
                StoragePackModel(pack_key=pack["pack_key"], worker_id=worker_id, byte_size=1),
            ]
        )
        session.commit()


def test_swarm_pass_keeps_the_rows_of_another_pass(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
    """Check that a pass's reset and cleanup delete only its own rows, and keep the rows of another invocation.

    Args:
        monkeypatch: Replaces the engine, the shard processes and the step log read.
        tmp_path: The pass folder.
    """
    engine = _named_sqlite(tmp_path / "db.sqlite", "loadtest_swarm")
    config = replace(_small_pass_config(), db_url="postgresql://user@localhost/loadtest_swarm")
    other = replace(config, stamp="otherpass")
    assert config.stamp != other.stamp
    _other_pass_rows(engine, other)
    other_rows = _rows(engine)
    _postgres_pass(monkeypatch, engine)
    _seed_rows_after_reset(monkeypatch, engine, swarm_10k.worker_id_for(config.stamp, 1, 0))
    monkeypatch.setattr(swarm_10k, "_digest", lambda shards: ("digest", 1))
    swarm_10k.run_pass(config, 1, tmp_path / "pass")
    rows = _rows(engine)
    assert {table: sorted(keys) for table, keys in rows.items()} == {
        table: sorted(other_rows[table] + _OTHER_ROWS[table]) for table in rows
    }
    assert all(swarm_10k.run_id_for(other.stamp, 0) in key for key in rows["agent_steps"])


def test_swarm_refuses_a_database_that_another_pass_holds(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
    """Check that a pass refuses a database whose swarm advisory lock another pass holds, before any reset.

    Args:
        monkeypatch: Replaces the engine and the shard processes, and makes a reset fail the test.
        tmp_path: The pass folder.
    """
    engine = _named_sqlite(tmp_path / "db.sqlite", "loadtest_swarm", lock_free=False)
    _postgres_pass(monkeypatch, engine)
    monkeypatch.setattr(swarm_10k, "_reset", _no_start)
    config = replace(_small_pass_config(), db_url="postgresql://user@localhost/loadtest_swarm")
    with pytest.raises(RuntimeError, match="another swarm pass runs"):
        swarm_10k.run_pass(config, 1, tmp_path / "pass")
    assert engine.pool.checkedin() == 0


@pytest.mark.parametrize("fails", [False, True])
def test_swarm_pass_frees_its_lock(monkeypatch: pytest.MonkeyPatch, tmp_path: Path, fails: bool) -> None:
    """Check that a pass frees its advisory lock when it ends, also when a shard fails.

    Args:
        monkeypatch: Replaces the engine, the shard processes and the step log read.
        tmp_path: The pass folder.
        fails: Whether the shard reports an error.
    """
    unlocks: list[int] = []
    engine = _named_sqlite(tmp_path / "db.sqlite", "loadtest_swarm", unlocks=unlocks)
    _postgres_pass(monkeypatch, engine)
    monkeypatch.setattr(swarm_10k, "_digest", lambda shards: ("digest", 1))
    if fails:
        monkeypatch.setattr(swarm_10k, "collect_reports", lambda *args: [{"worker_id": "swarm-w", "error": "broke"}])
    config = replace(_small_pass_config(), db_url="postgresql://user@localhost/loadtest_swarm")
    with pytest.raises(RuntimeError, match="broke") if fails else nullcontext():
        swarm_10k.run_pass(config, 1, tmp_path / "pass")
    assert unlocks == [swarm_10k.SWARM_LOCK_KEY]


@pytest.mark.parametrize("cleanup", ["_delete_swarm_rows", "_delete_packs"])
def test_swarm_pass_frees_its_lock_when_its_cleanup_fails(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path, cleanup: str
) -> None:
    """Check that a pass frees its advisory lock when its row or pack cleanup fails, and raises the cleanup error.

    Args:
        monkeypatch: Replaces the engine, the shard processes, the step log read and one cleanup step.
        tmp_path: The pass folder.
        cleanup: The cleanup step that fails.
    """
    unlocks: list[int] = []
    engine = _named_sqlite(tmp_path / "db.sqlite", "loadtest_swarm", unlocks=unlocks)
    _postgres_pass(monkeypatch, engine)
    monkeypatch.setattr(swarm_10k, "_digest", lambda shards: ("digest", 1))

    def fail(*args: Any) -> None:
        """Fail the cleanup step.

        Args:
            *args: Not used.

        Raises:
            OSError: Always.
        """
        raise OSError("cleanup failed")

    monkeypatch.setattr(swarm_10k, cleanup, fail)
    config = replace(_small_pass_config(), db_url="postgresql://user@localhost/loadtest_swarm")
    with pytest.raises(OSError, match="cleanup failed"):
        swarm_10k.run_pass(config, 1, tmp_path / "pass")
    assert unlocks == [swarm_10k.SWARM_LOCK_KEY]


def test_swarm_pass_that_fails_to_unlock_raises_and_disposes_every_engine(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    """Check that a failed unlock becomes the pass's cleanup error, and that every shard engine still gets disposed.

    Args:
        monkeypatch: Replaces the engine, the shard processes, the step log read and the engine's dispose.
        tmp_path: The pass folder.
    """
    engine = _named_sqlite(tmp_path / "db.sqlite", "loadtest_swarm", fails="pg_advisory_unlock")
    _postgres_pass(monkeypatch, engine)
    monkeypatch.setattr(swarm_10k, "_digest", lambda shards: ("digest", 1))
    disposes: list[Engine] = []
    dispose = engine.dispose

    def record_dispose() -> None:
        """Record one dispose, then dispose the engine."""
        disposes.append(engine)
        dispose()

    monkeypatch.setattr(engine, "dispose", record_dispose)
    config = replace(_small_pass_config(), db_url="postgresql://user@localhost/loadtest_swarm")
    with pytest.raises(OperationalError, match="pg_advisory_unlock"):
        swarm_10k.run_pass(config, 2, tmp_path / "pass")
    # Both shards of the pass get the one engine, so each of its 2 places in the pass's list gets a dispose.
    assert len(disposes) == 2
    assert engine.pool.checkedin() == 0


def test_swarm_lock_that_fails_closes_its_connection(tmp_path: Path) -> None:
    """Check that a lock call that fails gives its connection back and raises the error.

    Args:
        tmp_path: The database folder.
    """
    engine = _named_sqlite(tmp_path / "db.sqlite", "loadtest_swarm", fails="pg_try_advisory_lock")
    with pytest.raises(OperationalError, match="pg_try_advisory_lock"):
        swarm_10k._lock_database(engine)
    assert engine.pool.checkedout() == 0
    engine.dispose()


def test_swarm_worker_ids_of_two_passes_do_not_overlap() -> None:
    """Check that the 1-shard and the 2-shard passes of one invocation get different worker ids."""
    config = _swarm_config()
    single = {swarm_10k.worker_id_for(config.stamp, 1, index) for index in range(1)}
    sharded = {swarm_10k.worker_id_for(config.stamp, 2, index) for index in range(2)}
    assert not single & sharded


def test_swarm_steady_bounds_take_the_latest_first_step_and_the_earliest_last_step() -> None:
    """Check that one shard's steady part starts at its latest first step and ends at its earliest last step."""
    done = [
        [(0.0, 0.0, 0.0, 1.0), (0.0, 0.0, 0.0, 5.0)],
        [(0.0, 0.0, 0.0, 2.0), (0.0, 0.0, 0.0, 4.0)],
        [(0.0, 0.0, 0.0, 1.5), (0.0, 0.0, 0.0, 6.0)],
    ]
    assert swarm_10k.steady_bounds(done) == {"first_round_durable": 2.0, "first_agent_done": 4.0}


def _shard_report(times: list[float], first_round_durable: float, first_agent_done: float) -> dict[str, Any]:
    """Return a made-up shard report.

    Args:
        times: The moments the shard's steps were durable.
        first_round_durable: The moment the first step of every agent of the shard was durable.
        first_agent_done: The moment the first agent of the shard finished.

    Returns:
        The report, in the shape that ``_shard_process`` sends.
    """
    return {
        "worker_id": "swarm-w",
        "started": 0.0,
        "waits": [0.1] * len(times),
        "lates": [0.0] * len(times),
        "commit_calls": [0.0] * len(times),
        "times": times,
        "first_round_durable": first_round_durable,
        "first_agent_done": first_agent_done,
        "boxes_opened": 0,
        "boxes_open_max": 0,
    }


def test_swarm_steady_part_runs_from_the_last_first_round_to_the_first_done_agent(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    """Check that the steady part starts at the latest shard's first round and ends at the earliest done agent.

    Args:
        monkeypatch: Replaces the shard processes with two made-up reports, and the step log read.
        tmp_path: The pass folder.
    """
    monkeypatch.delenv("LOAD_TEST_SWARM_S3_BUCKET", raising=False)
    monkeypatch.setattr(swarm_10k, "AGENTS_PER_RUN", 10)
    context = SimpleNamespace(Barrier=lambda count: None, Queue=lambda: None, Process=_NoProcess)
    monkeypatch.setattr(swarm_10k, "multiprocessing", SimpleNamespace(get_context=lambda method: context))
    reports = [
        _shard_report([0.5, 1.5, 2.5, 3.5, 4.5, 5.5], first_round_durable=1.0, first_agent_done=5.0),
        _shard_report([1.0, 2.0, 3.0, 3.9, 4.1], first_round_durable=2.0, first_agent_done=4.0),
    ]
    monkeypatch.setattr(swarm_10k, "collect_reports", lambda *args: reports)
    monkeypatch.setattr(swarm_10k, "_digest", lambda shards: ("digest", 11))
    figures = swarm_10k.run_pass(_small_pass_config(), 2, tmp_path)
    # Only the steps after 2.0 s and up to 4.0 s count: 2.5, 3.0, 3.5 and 3.9.
    assert figures["steady_steps"] == 4
    assert figures["steady_seconds"] == 2.0
    assert figures["commits_per_second"] == 2.0
    keys = list(figures)
    assert keys.index("commits_per_second") == keys.index("steady_steps") + 1


def test_swarm_digest_hashes_each_step_body(tmp_path: Path) -> None:
    """Check that one changed byte in a step body changes the step log digest.

    Args:
        tmp_path: The folder for the database and the objects.
    """
    engine = create_engine(f"sqlite:///{tmp_path / 'db.sqlite'}")
    run_id = swarm_10k.run_id_for("test", 0)
    swarm_10k._reset(engine, [run_id])
    with Session(engine) as session:
        session.add(
            AgentStepModel(
                step_id=f"{run_id}:agent-00000:0",
                run_id=run_id,
                agent_id="agent-00000",
                seq=0,
                kind="model",
                execution_generation=1,
                grant_list_id="grants",
                pack_key="packs/w/0",
                pack_offset=2,
                pack_length=5,
            )
        )
        session.commit()
    objects = LocalObjectStore(tmp_path / "objects")
    shard = swarm_10k.Shard("sqlite", tmp_path, engine, objects, None, [run_id], "w")
    try:
        objects.put("packs/w/0", b"xxhelloxx")
        first = swarm_10k._digest([shard])
        objects.put("packs/w/0", b"xxhellpxx")
        second = swarm_10k._digest([shard])
        objects.put("packs/w/0", b"yyhelloyy")
        outside = swarm_10k._digest([shard])
    finally:
        engine.dispose()
    assert first[1] == second[1] == 1
    assert first[0] != second[0]
    # A byte outside the step's body is not part of the step log.
    assert outside == first


class _S3:
    """A stand-in S3 client whose ``delete_objects`` gives a fixed response."""

    def __init__(self, response: dict[str, Any]) -> None:
        """Keep the response.

        Args:
            response: What ``delete_objects`` returns.
        """
        self.response = response
        self.deleted: list[str] = []

    def delete_objects(self, Bucket: str, Delete: dict[str, Any]) -> dict[str, Any]:  # noqa: N803
        """Record the keys and return the response.

        Args:
            Bucket: Not used.
            Delete: The keys to delete.

        Returns:
            The fixed response.
        """
        self.deleted.extend(item["Key"] for item in Delete["Objects"])
        return self.response


@pytest.mark.parametrize(
    "response",
    [{}, {"Errors": []}, {"Errors": [{"Key": "packs/w/0", "Code": "AccessDenied", "Message": "no"}]}],
)
def test_swarm_pack_cleanup_fails_on_a_key_that_s3_did_not_delete(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path, response: dict[str, Any]
) -> None:
    """Check that the pack cleanup raises when S3 names a key that it did not delete, and only then.

    Args:
        monkeypatch: Sets the bucket.
        tmp_path: The shard folder.
        response: The S3 answer.
    """
    monkeypatch.setenv("LOAD_TEST_SWARM_S3_BUCKET", "swarm")
    s3 = _S3(response)
    objects = SimpleNamespace(list=lambda prefix: ["packs/w/0", "packs/w/1"])
    shard = swarm_10k.Shard("sqlite", tmp_path, None, objects, s3, [], "w")
    with (
        pytest.raises(RuntimeError, match=r"did not delete 1 swarm pack.*packs/w/0 \(AccessDenied\)")
        if response.get("Errors")
        else nullcontext()
    ):
        swarm_10k._delete_packs(shard)
    assert s3.deleted == ["packs/w/0", "packs/w/1"]


def test_run_all_starts_no_stack_for_in_process_scenarios(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
    """Check that ``run_all`` with only in-process scenarios starts, waits for and warms no stack.

    Args:
        monkeypatch: Sets the command line, and makes each stack step fail the test.
        tmp_path: The report folder.
    """
    monkeypatch.setattr(
        sys, "argv", ["run_all", "--scenarios=reference_shapes,chaos_gate,swarm_10k", f"--results-dir={tmp_path}"]
    )
    monkeypatch.setattr(run_all, "_ensure_env", lambda args: None)
    for name in ("_bring_up", "_wait_for_http", "warm_validation_cache", "_bring_down"):
        monkeypatch.setattr(run_all, name, _no_start)
    ran: list[str] = []
    result = ScenarioMetrics("reference_shapes").finish()
    result.slo_passed = True

    async def run_scenarios(names: list[str], api_url: str, mock_lm_url: str) -> list[Any]:
        """Record the scenario names and return one passed result.

        Args:
            names: The scenario names.
            api_url: Not used.
            mock_lm_url: Not used.

        Returns:
            The passed result.
        """
        ran.extend(names)
        return [result]

    monkeypatch.setattr(run_all, "_run_scenarios", run_scenarios)
    assert asyncio.run(run_all._async_main()) == 0
    assert ran == ["reference_shapes", "chaos_gate", "swarm_10k"]


def test_run_all_takes_the_swarm_database_only_from_the_environment(monkeypatch: pytest.MonkeyPatch) -> None:
    """Check that ``run_all`` has no flag for the swarm database URL, so a password stays off the command line.

    Args:
        monkeypatch: Sets the command line.
    """
    assert "LOAD_TEST_SWARM_DB_URL" not in {env_name for _, env_name, _ in run_all._SCENARIO_KNOBS}
    monkeypatch.setattr(sys, "argv", ["run_all", "--swarm-db-url=postgresql://user:secret@localhost/loadtest"])
    with pytest.raises(SystemExit):
        run_all._parse_args()


@pytest.mark.parametrize("digest", ["same", "other"])
def test_swarm_checks_only_the_step_log_of_the_two_shard_pass(monkeypatch: pytest.MonkeyPatch, digest: str) -> None:
    """Check that the 2-shard pass runs between the others and fails the gate only on another step log.

    Args:
        monkeypatch: Replaces ``run_pass`` and sets the backend.
        digest: The 2-shard pass's digest.
    """
    shard_counts: list[int] = []

    def fake_pass(config: swarm_10k.SwarmConfig, shard_count: int, folder: Path) -> dict[str, Any]:
        """Return good figures, with a low rate, a long wait and the digest on the 2-shard pass.

        Args:
            config: The swarm config.
            shard_count: The pass's shard count.
            folder: Not used.

        Returns:
            The figures.
        """
        shard_counts.append(shard_count)
        if shard_count != 2:
            return _pass_figures(shard_count)
        slow = {"mean": 5_000.0, "p50": 5_000.0, "p99": 9_000.0, "max": 9_500.0}
        return _pass_figures(2, commits_per_second=100.0, step_wait_ms=slow, digest=digest)

    monkeypatch.setattr(swarm_10k, "run_pass", fake_pass)
    result = swarm_10k.run(_target_backend(monkeypatch, _swarm_config(shards=13)))
    assert shard_counts == [1, 2, 13]
    assert result.extras["gated_pass_shards"] == 13
    assert [figures["shards"] for figures in result.extras["passes"]] == [1, 2, 13]
    assert result.extras["two_shard_same_step_log"] is (digest == "same")
    assert result.slo_violations == ([] if digest == "same" else ["the shard layouts gave different step logs"])


def test_swarm_with_two_shards_runs_the_two_shard_pass_once(monkeypatch: pytest.MonkeyPatch) -> None:
    """Check that a 2-shard swarm gates its 2-shard pass and does not run it twice.

    Args:
        monkeypatch: Replaces ``run_pass`` and sets the backend.
    """
    shard_counts: list[int] = []

    def fake_pass(config: swarm_10k.SwarmConfig, shard_count: int, folder: Path) -> dict[str, Any]:
        """Record the shard count and return good figures.

        Args:
            config: The swarm config.
            shard_count: The pass's shard count.
            folder: Not used.

        Returns:
            The figures.
        """
        shard_counts.append(shard_count)
        return _pass_figures(shard_count)

    monkeypatch.setattr(swarm_10k, "run_pass", fake_pass)
    result = swarm_10k.run(_target_backend(monkeypatch, _swarm_config(shards=2)))
    assert shard_counts == [1, 2]
    assert result.slo_passed


@pytest.mark.parametrize(
    ("db_url", "bucket"),
    [
        (None, ""),
        ("postgresql://user@localhost/loadtest_swarm", ""),
        (None, "swarm"),
        ("postgresql://user@localhost/loadtest_swarm", "swarm"),
    ],
)
def test_swarm_gate_needs_postgres_and_local_s3(
    monkeypatch: pytest.MonkeyPatch, db_url: str | None, bucket: str
) -> None:
    """Check that the gate passes only on Postgres and a local S3 bucket, and says so on any other backend.

    Args:
        monkeypatch: Replaces ``run_pass`` and sets the bucket.
        db_url: The database URL, or ``None`` for SQLite files.
        bucket: The S3 bucket, or empty for a local folder.
    """
    monkeypatch.setattr(swarm_10k, "run_pass", lambda config, shards, folder: _pass_figures(shards))
    monkeypatch.setenv("LOAD_TEST_SWARM_S3_BUCKET", bucket)
    result = swarm_10k.run(_swarm_config(db_url=db_url))
    proven = db_url is not None and bool(bucket)
    assert result.extras["target_backend"] is proven
    assert result.slo_passed is proven
    if not proven:
        assert result.slo_violations == [
            "the target is not proven on this backend: it needs Postgres (LOAD_TEST_SWARM_DB_URL)"
            " and local S3 (LOAD_TEST_SWARM_S3_BUCKET)"
        ]
