"""Chaos gate: the reference shapes must finish with a correct, replayable step log under kills (SKY-727).

The gate runs each reference shape (``reference_shapes.py``) several times
in parallel on the new run path. A chaos job thread picks a live run at
random and arms one kill:

- ``box``: the run's next box command finds its box gone. The tree sandbox
  turns that into a box failure, the step log commits the failure, and the
  engine scores the version as failed.
- ``worker``: the worker dies at its next model call or box command, as
  under SIGKILL, before the step commits. The run then moves to its next
  generation and a new worker resumes it from the committed steps.
- ``commit``: the worker dies right after a step commit returns and before
  the step is durable. ``EngineLoop`` waits for each step to be durable
  before it returns, so only this kill lands in that window. The step is
  only in the dead worker's buffer; the run moves to its next generation,
  the flush drops the step at the fence, and the resume does it again.

The gate passes only when 100% of the runs finish with the right result,
every agent's step log has no gap, a resumed worker does again only the
work that the killed worker did not make durable, a replay of each
finished run gives the same result with no model call, no box and no new
step, all three kill kinds fire, and the fence drops at least one step
that a commit kill cut off.

``failure_injection.py`` kills real stack containers; this gate kills
in-process boxes and workers, so it needs no stack and spends no money.
"""

from __future__ import annotations

import asyncio
import os
import random
import tempfile
import threading
import time
from concurrent.futures import ThreadPoolExecutor
from dataclasses import dataclass
from pathlib import Path
from typing import Any

from ..lib.metrics import ScenarioMetrics, ScenarioResult
from ..lib.reporter import print_result
from .reference_shapes import (
    DEFAULT_MODEL_WAIT_SECONDS,
    KILL_KINDS,
    SHAPES,
    ChaosSwitch,
    Harness,
    RunOutcome,
    Shape,
    box_runtime,
    outcome_report,
    run_shape,
)

# Two runs at once keeps the gate light on a shared laptop and still overlaps kills with live work.
DEFAULT_WORKERS = 2
# Each run starts its own threads, so the pool has a fixed cap whatever LOAD_TEST_CHAOS_WORKERS says.
MAX_WORKERS = 8


@dataclass
class ChaosGateConfig:
    """Knob set for one chaos gate run.

    Attributes:
        runs_per_shape: How many runs of each reference shape.
        rounds: The fake engine's model rounds for each run.
        workers: How many runs go at once; at most ``MAX_WORKERS`` start.
        kill_interval_seconds: The chaos job's mean wait between kills.
        seed: The chaos job's random seed. It fixes the order of the kill kinds and of the picks, but not when
            the kills fire, which depends on the threads.
        model_wait_seconds: The assumed wait of one real model call, for the old-path price.
    """

    runs_per_shape: int
    rounds: int
    workers: int
    kill_interval_seconds: float
    seed: int
    model_wait_seconds: float


class ChaosJob(threading.Thread):
    """A background job that arms a box, worker or commit kill on a random live run."""

    def __init__(self, switches: list[ChaosSwitch], interval: float, seed: int) -> None:
        """Bind the job to every run's switch.

        Args:
            switches: One chaos switch for each run.
            interval: The mean wait between kills, in seconds.
            seed: The random seed.
        """
        super().__init__(name="chaos-job", daemon=True)
        self._switches = switches
        self._interval = interval
        self._random = random.Random(seed)
        self._halt = threading.Event()
        self.armed: dict[str, int] = dict.fromkeys(KILL_KINDS, 0)

    def run(self) -> None:
        """Arm one kill after each random wait until the job stops."""
        while not self._halt.wait(self._random.uniform(0.5, 1.5) * self._interval):
            live = [switch for switch in self._switches if switch.live]
            if not live:
                continue
            kind = self._random.choice(KILL_KINDS)
            self._random.choice(live).arm(kind)
            self.armed[kind] += 1

    def stop(self) -> None:
        """Stop the job and wait for it."""
        self._halt.set()
        self.join()


def pool_size(workers: int) -> int:
    """Return how many runs go at once.

    Args:
        workers: The asked number of runs at once.

    Returns:
        At least 1 and at most ``MAX_WORKERS``.
    """
    return max(1, min(MAX_WORKERS, workers))


def run(config: ChaosGateConfig) -> ScenarioResult:
    """Run every shape under the chaos job and check each run's step log.

    Args:
        config: The knobs.

    Returns:
        The scenario result; ``slo_passed`` is ``True`` only when 100% of the runs finish correct and replayable.

    Raises:
        ValueError: When the kill interval is not above 0: the chaos job would wait 0 s and hold one core.
    """
    if config.kill_interval_seconds <= 0:
        raise ValueError(f"the chaos kill interval must be above 0 s, got {config.kill_interval_seconds}")
    runtime, provider = box_runtime()
    metrics = ScenarioMetrics("chaos_gate")
    plan: list[tuple[Shape, str, ChaosSwitch]] = [
        (shape, f"chaos-{index}-{repeat}", ChaosSwitch())
        for index, shape in enumerate(SHAPES)
        for repeat in range(config.runs_per_shape)
    ]
    outcomes: list[tuple[Shape, RunOutcome]] = []
    started = time.monotonic()
    with tempfile.TemporaryDirectory(prefix="sky727-chaos-") as folder:
        harness = Harness(Path(folder), runtime)
        chaos = ChaosJob([switch for _, _, switch in plan], config.kill_interval_seconds, config.seed)
        chaos.start()
        try:
            with ThreadPoolExecutor(max_workers=pool_size(config.workers), thread_name_prefix="chaos-run") as pool:
                futures = [
                    (shape, pool.submit(run_shape, harness, shape, run_id, rounds=config.rounds, switch=switch))
                    for shape, run_id, switch in plan
                ]
                outcomes = [(shape, future.result()) for shape, future in futures]
        finally:
            chaos.stop()
            harness.close()
    elapsed = time.monotonic() - started
    rows: list[dict[str, Any]] = []
    fired = dict.fromkeys(KILL_KINDS, 0)
    for (shape, outcome), (_, _, switch) in zip(outcomes, plan, strict=True):
        row = outcome_report(shape, outcome, config.model_wait_seconds)
        # The row's ``ok`` also holds the GPU placement check, so the metric and the gate agree.
        metrics.record(status_code=200 if row["ok"] else 500, latency_seconds=outcome.wall_seconds)
        row["shape"] = shape.name
        row["kills"] = dict(switch.fired)
        row["commit_cuts_lost"] = outcome.cuts_lost
        rows.append(row)
        for kind, count in switch.fired.items():
            fired[kind] += count
    passed = sum(1 for row in rows if row["ok"])
    result = metrics.finish()
    result.extras = {
        "box_provider": provider,
        "gpu_provider": "fake-modal (placement check, local box)",
        "model": "in-process mock",
        "runs": len(rows),
        "runs_ok": passed,
        "runs_ok_pct": round(100 * passed / len(rows), 2) if rows else 0.0,
        "kills_armed": chaos.armed,
        "kills_fired": fired,
        "commit_cuts_lost": sum(row["commit_cuts_lost"] for row in rows),
        "worker_restarts": sum(row["restarts"] for row in rows),
        "elapsed_seconds": round(elapsed, 3),
        "seed": config.seed,
        "assumed_model_wait_seconds": config.model_wait_seconds,
        "runs_detail": rows,
    }
    result.slo_violations = [
        f"{row['run_id']} ({row['shape']}) did not finish correct and replayable: {row.get('error', 'log check')}"
        for row in rows
        if not row["ok"]
    ]
    if not all(fired.values()):
        result.slo_violations.append(f"the chaos job did not fire every kill kind: {fired}")
    elif result.extras["commit_cuts_lost"] == 0:
        result.slo_violations.append("no commit kill landed before the flush, so the fence dropped no step")
    result.slo_passed = not result.slo_violations
    return result


def default_config() -> ChaosGateConfig:
    """Return the knob set from the environment.

    Returns:
        The config: ``LOAD_TEST_CHAOS_RUNS_PER_SHAPE`` (default 3), ``LOAD_TEST_SHAPE_ROUNDS`` (default 4),
        ``LOAD_TEST_CHAOS_WORKERS`` (default 2), ``LOAD_TEST_CHAOS_KILL_SECONDS`` (default 0.3),
        ``LOAD_TEST_CHAOS_SEED`` (default 727) and ``LOAD_TEST_MODEL_WAIT_SECONDS`` (default 20).
    """
    return ChaosGateConfig(
        runs_per_shape=int(os.environ.get("LOAD_TEST_CHAOS_RUNS_PER_SHAPE", "3")),
        rounds=int(os.environ.get("LOAD_TEST_SHAPE_ROUNDS", "4")),
        workers=int(os.environ.get("LOAD_TEST_CHAOS_WORKERS", str(DEFAULT_WORKERS))),
        kill_interval_seconds=float(os.environ.get("LOAD_TEST_CHAOS_KILL_SECONDS", "0.3")),
        seed=int(os.environ.get("LOAD_TEST_CHAOS_SEED", "727")),
        model_wait_seconds=float(os.environ.get("LOAD_TEST_MODEL_WAIT_SECONDS", str(DEFAULT_MODEL_WAIT_SECONDS))),
    )


async def main(api_base_url: str, mock_lm_url: str) -> ScenarioResult:
    """Run the gate in a thread and print the console report.

    Args:
        api_base_url: Not used: the gate needs no stack.
        mock_lm_url: Not used: the model is in-process.

    Returns:
        The scenario result.
    """
    result = await asyncio.to_thread(run, default_config())
    print_result(result)
    return result


if __name__ == "__main__":
    asyncio.run(main("", ""))
