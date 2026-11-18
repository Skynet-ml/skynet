"""The SKY-727 load-test harness: thread caps, the swarm gate, shard failures and the resume check."""

import queue
import sys
import threading
import time
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path
from typing import Any

import pytest
from sqlalchemy import create_engine

from core.service_gateway.optimization.blackbox.sandbox_runtime import LocalSubprocessRuntime
from core.storage.object_store import LocalObjectStore
from core.storage.step_log_store import StepLogStore
from core.worker.engine_loop import EngineLoop
from load_tests import run_all
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
        "digest": "same",
    }
    figures.update(overrides)
    return figures


def _gate(monkeypatch: pytest.MonkeyPatch, config: swarm_10k.SwarmConfig, **sharded: Any) -> list[str]:
    """Run the swarm gate on made-up pass figures and return its violations.

    Args:
        monkeypatch: Replaces ``run_pass``.
        config: The swarm config.
        **sharded: The figures to change on the sharded pass.

    Returns:
        The gate's violations.
    """

    def fake_pass(config: swarm_10k.SwarmConfig, shard_count: int, folder: Path) -> dict[str, Any]:
        """Return made-up figures, with the changes on the sharded pass.

        Args:
            config: The swarm config.
            shard_count: The pass's shard count.
            folder: Not used.

        Returns:
            The figures.
        """
        return _pass_figures(shard_count, **(sharded if shard_count > 1 else {}))

    monkeypatch.setattr(swarm_10k, "run_pass", fake_pass)
    result = swarm_10k.run(config)
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
    offsets = [swarm_10k.start_offset("swarm-000000", f"agent-{i:05d}", interval) for i in range(50)]
    assert all(0 <= offset < interval for offset in offsets)
    assert len(set(offsets)) == len(offsets)
    assert offsets == [swarm_10k.start_offset("swarm-000000", f"agent-{i:05d}", interval) for i in range(50)]


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
        ({"commits_per_second": 5_000.0}, "steady commit rate"),
        ({"step_wait_ms": {"mean": 900.0, "p50": 900.0, "p99": 1_200.0, "max": 1_300.0}}, "step wait p99"),
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
    run_id = swarm_10k.run_id_for(0)
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
