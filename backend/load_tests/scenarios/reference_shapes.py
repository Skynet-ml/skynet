"""Reference shapes on the new run path, with a mock model and local boxes (SKY-727).

The scenario runs four small reference shapes on the worker's engine loop
(SKY-719) over the actor tree (SKY-717), the step log and the tree sandbox:
circle packing, llama.cpp, repo test speed and KernelBench level 1. The
engine ports (SKY-720 to SKY-724) are not done yet, so a small fake engine
stands in for each shape: it scores the seed, then asks the mock model for a
new version in each round and scores that.

Nothing here spends money. The model is an in-process mock. The CPU shapes
open their boxes on the local Docker provider when ``LOAD_TEST_BOX_IMAGE``
names a local image id, and on local subprocess boxes when it does not. The
GPU shape opens its boxes through a fake Modal runtime that refuses a box
without a ``gpu:<type>`` placement, and then opens the same local box.

The database is a SQLite file and the object store is a local folder, both
in a temporary folder, so the scenario needs no running stack.

The report prices sandbox use per run from the metered box-seconds and list
prices: the new path opens a box only for a step, and the old path kept one
box open for the whole run, also while the engine waited for the model.

The chaos gate (``chaos_gate.py``) reuses this harness.
"""

from __future__ import annotations

import asyncio
import os
import re
import shlex
import tempfile
import threading
import time
from collections import Counter
from collections.abc import Callable, Mapping
from dataclasses import dataclass, field, replace
from decimal import Decimal
from pathlib import Path
from typing import Any

from sqlalchemy import create_engine, func, select, update
from sqlalchemy.engine import Engine
from sqlalchemy.orm import Session

from core.billing.budgets import BudgetService
from core.billing.vercel_usage import vercel_actual_usd
from core.config import Settings
from core.service_gateway.optimization.blackbox.protocol import EngineContext, EvalServer, ModelReply, Result, Task
from core.service_gateway.optimization.blackbox.sandbox_contract import (
    CommandResult,
    SandboxRuntime,
    SandboxSession,
    SandboxSpec,
)
from core.service_gateway.optimization.blackbox.sandbox_runtime import AsyncBridgeRuntime, LocalSubprocessRuntime
from core.service_gateway.optimization.blackbox.tree_sandbox import RuntimeBoxSource
from core.storage.models import (
    AgentStepModel,
    Base,
    BillingCustomerModel,
    JobModel,
    StoragePackModel,
    TreeObjectModel,
)
from core.storage.object_store import LocalObjectStore
from core.storage.step_log_store import StepLogStore
from core.storage.tree_store import TreeEntry, TreeStore
from core.storage.workspace_store import WorkspaceStore
from core.worker.actors import User
from core.worker.engine_loop import EngineLoop

from ..lib.metrics import ScenarioMetrics, ScenarioResult
from ..lib.reporter import print_result

GRANT_LIST_ID = "grants-loadtest"
SEED_CANDIDATE = "1"
# One lease is 10 cents and a mock model call costs a quarter cent, so a run never runs out of lease room.
CHUNK_CENTS = 10
LEASE_SLICES = 4
MODEL_MAX_CENTS = 2
MODEL_COST_CENTS = Decimal("0.25")
# Each worker incarnation reserves one pool of leases that a killed worker never returns, so a chaos run needs room.
BUDGET_CENTS = 5_000
WALLET_CENTS = 50_000
# Short, so each step's durable wait is short; the swarm scenario measures the flush window on its own.
FLUSH_WINDOW_SECONDS = 0.02
# The code default, not ``settings``: the harness must not take a size cap from ``backend/.env``.
FLUSH_MAX_BYTES = Settings.model_fields["step_log_flush_max_bytes"].default
BOX_LIFETIME_SECONDS = 300
# Vercel's public Pro rates in iad1 (core/billing/vercel_usage.py) price the CPU shapes.
VERCEL_REGION = "iad1"
# Modal's public list prices, in USD per second. The code base has no Modal price table, so these are the
# harness's own copy: check them against modal.com/pricing before a decision depends on them.
MODAL_GPU_USD_PER_SECOND = {
    "T4": Decimal("0.000164"),
    "L4": Decimal("0.000222"),
    "A10G": Decimal("0.000306"),
    "A100": Decimal("0.000694"),
    "H100": Decimal("0.001097"),
}
MODAL_CPU_CORE_USD_PER_SECOND = Decimal("0.0000131")
# The mock model answers at once. A real reflection call takes seconds, and the old path held its box open
# during that wait, so the old-path price adds this wait for each model call.
DEFAULT_MODEL_WAIT_SECONDS = 20.0
_PLACEMENT = re.compile(r"gpu:([A-Za-z0-9]+)")


class WorkerKilledError(BaseException):
    """The chaos job killed the worker: like a SIGKILL, no handler of ``Exception`` sees it."""


class ReplayLeakError(BaseException):
    """A replay asked for real work: a model call or a box that the committed log should answer."""


@dataclass(frozen=True)
class Shape:
    """One reference shape: its seed workspace, its step command, its metric and its box size.

    Attributes:
        name: The shape name.
        work_script: The seed tree's ``work.sh``; it writes ``answer.txt`` from the candidate in ``$1``.
        metric_script: The metric tree's ``score.awk``; it prints the score from ``outputs/answer.txt``.
        vcpus: The box size.
        gpu: The GPU type of the ``gpu:<type>`` placement, or ``None`` for a CPU shape.
    """

    name: str
    work_script: str
    metric_script: str
    vcpus: int
    gpu: str | None = None

    def command(self, candidate: str) -> str:
        """Return the step command for one candidate.

        Args:
            candidate: The candidate text.

        Returns:
            The shell command.
        """
        return f"sh work.sh {shlex.quote(candidate)}"

    @property
    def metric_command(self) -> str:
        """Return the metric command; its last stdout line is the score."""
        return "awk -f metric/score.awk outputs/answer.txt"


SHAPES: tuple[Shape, ...] = (
    Shape(
        "circle_packing",
        work_script=(
            'awk -v n="$1" \'BEGIN { r = 1 / (2 * sqrt(n)); printf "%d %.6f\\n", n, n * 3.14159265 * r * r * (1 - n / 40) }\''
            " > answer.txt\n"
        ),
        metric_script='{ printf "%.6f\\n", $2 }\n',
        vcpus=2,
    ),
    Shape(
        "llama_cpp",
        work_script=(
            'awk -v t="$1" \'BEGIN { printf "%.3f\\n", 100 * t / (1 + (t - 4) * (t - 4) / 8) }\' > answer.txt\n'
        ),
        metric_script='{ printf "%.3f\\n", $1 }\n',
        vcpus=4,
    ),
    Shape(
        "repo_test_speed",
        work_script='awk -v w="$1" \'BEGIN { printf "%.3f\\n", 60 / w + w }\' > answer.txt\n',
        metric_script='{ printf "%.3f\\n", 100 - $1 }\n',
        vcpus=2,
    ),
    Shape(
        "kernelbench_l1",
        work_script='awk -v b="$1" \'BEGIN { printf "%.4f\\n", 1 / (0.2 + 1 / (b * 16)) }\' > answer.txt\n',
        metric_script='{ printf "%.4f\\n", $1 }\n',
        vcpus=4,
        gpu="A10G",
    ),
)


class ChaosSwitch:
    """One run's chaos fuse: the chaos job arms a kill, and the run's next model call or box command fires it."""

    def __init__(self) -> None:
        """Start unarmed and not live."""
        self._lock = threading.Lock()
        self._armed: str | None = None
        self.live = False
        self.fired: Counter[str] = Counter()

    def arm(self, kind: str) -> None:
        """Arm one kill.

        Args:
            kind: ``box`` kills the next box command; ``worker`` kills the worker at its next model call or box
                command.
        """
        with self._lock:
            self._armed = kind

    def take(self, where: str) -> str | None:
        """Fire the armed kill when it applies at this point.

        Args:
            where: ``model`` or ``box``.

        Returns:
            The kill that fired, or ``None``.
        """
        with self._lock:
            kind = self._armed
            if kind is None or (kind == "box" and where != "box"):
                return None
            self._armed = None
            self.fired[kind] += 1
            return kind

    def disarm(self) -> None:
        """Drop an armed kill that did not fire."""
        with self._lock:
            self._armed = None


class MockModel:
    """A deterministic mock model: ``improve <n>`` gets ``n + 1``. It makes no network call."""

    def __init__(self, switch: ChaosSwitch | None = None, *, refuse: bool = False) -> None:
        """Bind the model to a run's chaos switch.

        Args:
            switch: The run's chaos switch, or ``None`` for no chaos.
            refuse: Raise :class:`ReplayLeakError` on every call, for a replay check.
        """
        self._switch = switch
        self._refuse = refuse
        self._lock = threading.Lock()
        self.calls = 0

    def __call__(self, prompt: str | list[dict[str, Any]], tools: list[dict[str, Any]] | None = None) -> ModelReply:
        """Answer one prompt.

        Args:
            prompt: ``improve <n>``.
            tools: Not used: the reference engine calls the model with no tools.

        Returns:
            The next candidate and the call's cost in cents, with no tool call.

        Raises:
            ReplayLeakError: When the model refuses every call.
            WorkerKilledError: When the chaos job killed the worker.
        """
        if self._refuse:
            raise ReplayLeakError("the replay called the model")
        if self._switch is not None and self._switch.take("model") == "worker":
            raise WorkerKilledError
        with self._lock:
            self.calls += 1
        return ModelReply(text=str(int(str(prompt).split()[-1]) + 1), cost_cents=MODEL_COST_CENTS)


class BoxMeter:
    """Meter every box of one run: its lifetime, the open count and the GPU placements."""

    def __init__(self) -> None:
        """Start with no box."""
        self._lock = threading.Lock()
        self.lifetimes: list[float] = []
        self.opened = 0
        self.open_now = 0
        self.open_max = 0
        self.placements: Counter[str] = Counter()

    def started(self) -> float:
        """Count one open box.

        Returns:
            The start time.
        """
        with self._lock:
            self.opened += 1
            self.open_now += 1
            self.open_max = max(self.open_max, self.open_now)
        return time.monotonic()

    def stopped(self, started_at: float) -> None:
        """Count one closed box and keep its lifetime.

        Args:
            started_at: The time :meth:`started` gave.
        """
        with self._lock:
            self.open_now -= 1
            self.lifetimes.append(time.monotonic() - started_at)

    def placed(self, placement: str) -> None:
        """Count one checked GPU placement.

        Args:
            placement: The ``gpu:<type>`` placement.
        """
        with self._lock:
            self.placements[placement] += 1

    @property
    def box_seconds(self) -> float:
        """Return the metered box-seconds of every closed box."""
        with self._lock:
            return sum(self.lifetimes)


class FakeModalRuntime:
    """A fake Modal runtime: it checks the ``gpu:<type>`` placement, then opens a local box.

    No Modal call happens. A spec without a valid placement fails the open, so the step fails.
    """

    injects_headers = False

    def __init__(self, inner: SandboxRuntime, meter: BoxMeter) -> None:
        """Bind the fake to the local runtime that really opens the box.

        Args:
            inner: The local runtime.
            meter: Counts the placements.
        """
        self._inner = inner
        self._meter = meter

    def open(self, spec: SandboxSpec) -> SandboxSession:
        """Check the placement and open a local box.

        Args:
            spec: The box spec; ``tags["placement"]`` must be ``gpu:<type>`` with a priced type.

        Returns:
            The local box.

        Raises:
            ValueError: When the placement is missing or names an unknown GPU type.
        """
        placement = spec.tags.get("placement", "")
        match = _PLACEMENT.fullmatch(placement)
        if match is None or match.group(1) not in MODAL_GPU_USD_PER_SECOND:
            raise ValueError(f"the fake Modal runtime needs a gpu:<type> placement, got {placement!r}")
        self._meter.placed(placement)
        tags = {key: value for key, value in spec.tags.items() if key != "placement"}
        return self._inner.open(replace(spec, tags=tags))


class _MeteredBox:
    """A box that the meter times and the chaos job can kill."""

    def __init__(self, inner: SandboxSession, switch: ChaosSwitch | None) -> None:
        """Wrap one open box.

        Args:
            inner: The open box.
            switch: The run's chaos switch, or ``None``.
        """
        self._inner = inner
        self._switch = switch
        self._killed = False

    def write_files(self, files: Mapping[str, str]) -> None:
        """Write files into the box.

        Args:
            files: Relative path to content.
        """
        self._inner.write_files(files)

    def run(self, command: str, **options: Any) -> CommandResult:
        """Run a command, unless the chaos job kills the box or the worker first.

        Args:
            command: The shell command.
            **options: The other :meth:`SandboxSession.run` options.

        Returns:
            The command result.

        Raises:
            ConnectionError: When the chaos job killed the box.
            WorkerKilledError: When the chaos job killed the worker.
        """
        kill = self._switch.take("box") if self._switch is not None else None
        if kill == "worker":
            raise WorkerKilledError
        if kill == "box":
            self._killed = True
            self._inner.close()
            raise ConnectionError("the chaos job killed the box")
        return self._inner.run(command, **options)

    def read_file(self, path: str) -> str | None:
        """Read a file from the box.

        Args:
            path: Relative file path.

        Returns:
            The text, or ``None``.
        """
        return self._inner.read_file(path)

    def close(self) -> None:
        """Close the box; a box that the chaos job killed is already gone."""
        if not self._killed:
            self._inner.close()


class MeteredSource(RuntimeBoxSource):
    """A fresh box for each step, as in production, with a meter and a chaos switch on each box."""

    def __init__(
        self,
        runtime: SandboxRuntime,
        spec: SandboxSpec,
        meter: BoxMeter,
        switch: ChaosSwitch | None = None,
        *,
        refuse: bool = False,
    ) -> None:
        """Bind the source to a runtime, its spec, the run's meter and the run's chaos switch.

        Args:
            runtime: Opens the boxes.
            spec: The box spec.
            meter: Meters the boxes.
            switch: The run's chaos switch, or ``None``.
            refuse: Raise :class:`ReplayLeakError` on every acquire, for a replay check.
        """
        super().__init__(runtime, spec)
        self._meter = meter
        self._switch = switch
        self._refuse = refuse
        self._started: dict[int, float] = {}

    def acquire(self, agent_id: str, grants: Any) -> SandboxSession:
        """Open and meter one box.

        Args:
            agent_id: The agent that runs the step.
            grants: The step's grants.

        Returns:
            The box.

        Raises:
            ReplayLeakError: When the source refuses every box.
        """
        if self._refuse:
            raise ReplayLeakError("the replay opened a box")
        box = _MeteredBox(super().acquire(agent_id, grants), self._switch)
        self._started[id(box)] = self._meter.started()
        return box

    def release(self, box: SandboxSession, healthy: bool) -> None:
        """Close the box and stop its meter.

        Args:
            box: The box.
            healthy: Whether the step finished.
        """
        try:
            super().release(box, healthy)
        finally:
            self._meter.stopped(self._started.pop(id(box)))


class FakeEngine:
    """A small deterministic engine: it scores the seed, then asks the model for a new version each round."""

    name = "fake"

    def __init__(self, rounds: int) -> None:
        """Set the number of rounds.

        Args:
            rounds: How many model calls the engine makes.
        """
        self.rounds = rounds

    def run(self, task: Task, server: EvalServer, ctx: EngineContext) -> Result:
        """Improve the seed one round at a time.

        Args:
            task: The task with the seed.
            server: The eval server.
            ctx: The loop's context.

        Returns:
            The best version and its score.
        """
        candidate = best = str(task.seed_candidate)
        best_score = server.evaluate(candidate)[0]
        for _ in range(self.rounds):
            candidate = ctx.reflection_lm(f"improve {candidate}")
            score = server.evaluate(candidate)[0]
            if score is not None and (best_score is None or score > best_score):
                best, best_score = candidate, score
        return Result(best, best_score, server.used)


@dataclass
class RunOutcome:
    """What one shape run did, and whether its step log is correct and replays.

    Attributes:
        run_id: The job id.
        shape: The shape name.
        finished: Whether the engine finished.
        result: ``(best candidate, best score, evals)``, or ``None``.
        wall_seconds: The run's wall time, restarts included.
        restarts: How many killed workers the run resumed from.
        model_calls: The real model calls; replayed calls do not count.
        steps: The committed steps.
        gapless: Whether every agent's seq runs from 0 with no gap.
        replayed: Whether a replay gave the same result with no model call, no box and no new step.
        error: The error that stopped the run, or ``None``.
        meter: The run's box meter.
    """

    run_id: str
    shape: str
    finished: bool = False
    result: tuple[str, float | None, int] | None = None
    wall_seconds: float = 0.0
    restarts: int = 0
    model_calls: int = 0
    steps: int = 0
    gapless: bool = False
    replayed: bool = False
    error: str | None = None
    meter: BoxMeter = field(default_factory=BoxMeter)

    @property
    def ok(self) -> bool:
        """Return whether the run finished with a correct, replayable step log."""
        return self.finished and self.gapless and self.replayed


def box_runtime() -> tuple[SandboxRuntime, str]:
    """Return the local box runtime: Docker when ``LOAD_TEST_BOX_IMAGE`` is set, else local subprocesses.

    Returns:
        The runtime and its name for the report.
    """
    image = os.environ.get("LOAD_TEST_BOX_IMAGE", "").strip()
    if image:
        return AsyncBridgeRuntime("docker", image=image), "docker"
    return LocalSubprocessRuntime(), "local-subprocess"


class Harness:
    """One SQLite database and one object folder for a set of shape runs, with no stack."""

    def __init__(self, folder: Path, runtime: SandboxRuntime) -> None:
        """Create the database and the object store in a folder.

        Args:
            folder: A private, empty folder.
            runtime: The local box runtime.
        """
        self.folder = folder
        self.runtime = runtime
        self.engine: Engine = create_engine(f"sqlite:///{folder / 'db.sqlite'}", connect_args={"timeout": 60})
        Base.metadata.create_all(self.engine)
        self.objects = LocalObjectStore(folder / "objects")
        self._owners: dict[str, tuple[User, int]] = {}
        self._trees: dict[str, tuple[str, str]] = {}

    def close(self) -> None:
        """Close the database connections."""
        self.engine.dispose()

    def add_run(self, run_id: str, shape: Shape) -> None:
        """Create a funded user, a running job, a run budget and the shape's seed and metric trees for one run.

        The trees go in once, before the first worker, as the submit path stores them. A worker incarnation
        that stored them again would add a pack, and the replay check would not see that its log is unchanged.

        Args:
            run_id: The job id.
            shape: The shape.
        """
        username = f"lt-{run_id}"
        with Session(self.engine) as session:
            session.add(
                BillingCustomerModel(
                    username=username,
                    stripe_customer_id=f"local-{username}",
                    balance_cents=WALLET_CENTS,
                    grant_remaining=0,
                )
            )
            session.add(JobModel(optimization_id=run_id, username=username, status="running", execution_generation=1))
            session.commit()
        budgets = BudgetService(engine=self.engine)
        budget = budgets.create(username, BUDGET_CENTS, idempotency_key=f"{run_id}-budget")
        budget = budgets.attach_to_job(budget.id, username, run_id, expected_revision=budget.revision)
        user = User(username, engine=self.engine, budgets=budgets)
        WorkspaceStore(self.engine).set_budget(user.workspace().id, budget.id)
        self._owners[run_id] = (user, budget.generation)
        store = self._step_log(f"{run_id}-setup")
        try:
            trees = TreeStore(store)
            seed_tree = trees.put_tree(
                run_id, [TreeEntry("work.sh", "file", trees.put_blob(run_id, shape.work_script.encode()))]
            )
            metric_tree = trees.put_tree(
                run_id, [TreeEntry("score.awk", "file", trees.put_blob(run_id, shape.metric_script.encode()))]
            )
        finally:
            store.close()
        self._trees[run_id] = (seed_tree, metric_tree)

    def _step_log(self, worker_id: str) -> StepLogStore:
        """Start one step log with the harness's flush window and size cap.

        Args:
            worker_id: The worker id; it starts with the run id, so :meth:`footprint` finds its packs.

        Returns:
            The step log.
        """
        return StepLogStore(
            self.engine,
            self.objects,
            worker_id,
            flush_window_seconds=FLUSH_WINDOW_SECONDS,
            flush_max_bytes=FLUSH_MAX_BYTES,
        )

    def new_generation(self, run_id: str) -> int:
        """Move the job to its next generation, as the orphan sweep does after a worker dies.

        Args:
            run_id: The job id.

        Returns:
            The new generation.
        """
        with Session(self.engine) as session:
            generation = session.scalar(select(JobModel.execution_generation).where(JobModel.optimization_id == run_id))
            session.execute(
                update(JobModel).where(JobModel.optimization_id == run_id).values(execution_generation=generation + 1)
            )
            session.commit()
        return int(generation) + 1

    def steps(self, run_id: str) -> list[tuple[str, int, str, int]]:
        """Return the run's committed steps.

        Args:
            run_id: The job id.

        Returns:
            ``(agent_id, seq, kind, execution_generation)`` for each step, in order.
        """
        with Session(self.engine) as session:
            rows = session.execute(
                select(
                    AgentStepModel.agent_id,
                    AgentStepModel.seq,
                    AgentStepModel.kind,
                    AgentStepModel.execution_generation,
                )
                .where(AgentStepModel.run_id == run_id)
                .order_by(AgentStepModel.agent_id, AgentStepModel.seq)
            )
            return [(row.agent_id, row.seq, row.kind, row.execution_generation) for row in rows]

    def footprint(self, run_id: str) -> tuple[int, int, int, int]:
        """Count what the run stored: its steps, its pack rows, its tree object rows and its pack objects.

        Args:
            run_id: The job id.

        Returns:
            ``(agent_steps, storage_packs, tree_objects, pack objects)``.
        """
        workers = f"{run_id}-"
        with Session(self.engine) as session:
            steps = session.scalar(select(func.count()).where(AgentStepModel.run_id == run_id))
            packs = session.scalar(select(func.count()).where(StoragePackModel.worker_id.startswith(workers)))
            objects = session.scalar(select(func.count()).where(TreeObjectModel.username == f"lt-{run_id}"))
        return int(steps), int(packs), int(objects), len(self.objects.list(f"packs/{workers}"))

    def loop(
        self,
        shape: Shape,
        run_id: str,
        generation: int,
        model: MockModel,
        meter: BoxMeter,
        switch: ChaosSwitch | None = None,
        *,
        refuse: bool = False,
    ) -> tuple[EngineLoop, StepLogStore, Callable[..., Any]]:
        """Start one worker incarnation of a run: its own step log, run actor, loop and scorer.

        Args:
            shape: The shape.
            run_id: The job id.
            generation: The job's current generation.
            model: The model.
            meter: The run's box meter.
            switch: The run's chaos switch, or ``None``.
            refuse: Refuse every box, for a replay check.

        Returns:
            The loop, its step log and the step scorer.
        """
        user, budget_generation = self._owners[run_id]
        # Each incarnation is a new worker with its own step log, as after a pod restart under a new name.
        store = self._step_log(f"{run_id}-g{generation}")
        trees = TreeStore(store)
        seed_tree, metric_tree = self._trees[run_id]
        run = user.workspace().start_run(
            run_id,
            step_log=store,
            execution_generation=generation,
            budget_generation=budget_generation,
            grant_list_id=GRANT_LIST_ID,
            chunk_cents=CHUNK_CENTS,
            lease_slices=LEASE_SLICES,
        )
        spec = SandboxSpec(
            lifetime_seconds=BOX_LIFETIME_SECONDS,
            vcpus=shape.vcpus,
            operation_key=f"loadtest-{run_id}",
            tags={"placement": f"gpu:{shape.gpu}"} if shape.gpu else {},
        )
        runtime = FakeModalRuntime(self.runtime, meter) if shape.gpu else self.runtime
        source = MeteredSource(runtime, spec, meter, switch, refuse=refuse)
        loop = EngineLoop(
            run,
            engine=self.engine,
            trees=trees,
            candidates=source,
            metrics=source,
            model=model,
            model_max_cents=MODEL_MAX_CENTS,
            run_dir=self.folder / run_id / f"g{generation}",
        )

        def scorer(loop: EngineLoop, candidate: Any, case: Any) -> tuple[float | None, dict[str, Any]]:
            """Run the shape's command in one box, then its metric in another.

            Args:
                loop: The engine loop.
                candidate: The candidate text.
                case: Not used.

            Returns:
                The score and the side information.
            """
            result, tree_id = loop.run(seed_tree, shape.command(str(candidate)))
            if not result.ok:
                return None, {"exit_code": result.exit_code}
            return loop.score(tree_id, metric_tree, shape.metric_command), {}

        return loop, store, scorer


def _close_quietly(loop: EngineLoop | None, store: StepLogStore) -> None:
    """Close a loop and its step log, and keep going when a close fails.

    Args:
        loop: The loop, or ``None`` for a dead worker that never closes its run.
        store: The step log.
    """
    if loop is not None:
        try:
            loop.close()
        except Exception as error:
            print(f"[loadtest] run close failed: {type(error).__name__}: {error}")
    try:
        store.close()
    except Exception as error:
        print(f"[loadtest] step log close failed: {type(error).__name__}: {error}")


def _gapless(steps: list[tuple[str, int, str, int]]) -> bool:
    """Return whether each agent's seq runs from 0 with no gap.

    Args:
        steps: ``(agent_id, seq, kind, generation)``, ordered by agent and seq.

    Returns:
        Whether the log has no gap.
    """
    seqs: dict[str, list[int]] = {}
    for agent_id, seq, _, _ in steps:
        seqs.setdefault(agent_id, []).append(seq)
    return bool(seqs) and all(found == list(range(len(found))) for found in seqs.values())


def run_shape(
    harness: Harness,
    shape: Shape,
    run_id: str,
    *,
    rounds: int,
    switch: ChaosSwitch | None = None,
    max_restarts: int = 20,
) -> RunOutcome:
    """Run one shape to the end, resume after each killed worker, then check the step log.

    Args:
        harness: The database and object store.
        shape: The shape.
        run_id: The job id.
        rounds: The fake engine's model rounds.
        switch: The run's chaos switch, or ``None`` for no chaos.
        max_restarts: The most killed workers that the run resumes from.

    Returns:
        The run's outcome.
    """
    outcome = RunOutcome(run_id, shape.name)
    harness.add_run(run_id, shape)
    model = MockModel(switch)
    generation = 1
    started = time.monotonic()
    if switch is not None:
        switch.live = True
    try:
        while outcome.result is None and outcome.error is None:
            loop, store, scorer = harness.loop(shape, run_id, generation, model, outcome.meter, switch)
            try:
                result = FakeEngine(rounds).run(
                    Task(SEED_CANDIDATE), loop.server(scorer, max_evals=rounds + 1), loop.context()
                )
            except WorkerKilledError:
                outcome.restarts += 1
                # The sweep moves the generation first, so the dead worker's buffered steps fail the fence at
                # the close's flush, as if the kill lost them.
                generation = harness.new_generation(run_id)
                _close_quietly(None, store)
                if outcome.restarts > max_restarts:
                    outcome.error = f"more than {max_restarts} restarts"
                continue
            except Exception as error:
                outcome.error = f"{type(error).__name__}: {error}"
                _close_quietly(loop, store)
                break
            outcome.result = (str(result.best_candidate), result.best_score, result.total_evals)
            _close_quietly(loop, store)
    finally:
        if switch is not None:
            switch.live = False
            switch.disarm()
    outcome.wall_seconds = time.monotonic() - started
    outcome.model_calls = model.calls
    outcome.finished = outcome.result is not None
    steps = harness.steps(run_id)
    outcome.steps = len(steps)
    outcome.gapless = _gapless(steps)
    if outcome.finished:
        outcome.replayed = _replays(harness, shape, run_id, rounds, outcome.result)
    return outcome


def _replays(
    harness: Harness,
    shape: Shape,
    run_id: str,
    rounds: int,
    expected: tuple[str, float | None, int] | None,
) -> bool:
    """Resume the finished run on a new generation with no model and no box, and compare the result.

    Args:
        harness: The database and object store.
        shape: The shape.
        run_id: The job id.
        rounds: The fake engine's model rounds.
        expected: The live run's result.

    Returns:
        Whether the replay gave the same result with no model call, no box, and no new step, pack row, tree
        object row or pack object.
    """
    before = harness.footprint(run_id)
    generation = harness.new_generation(run_id)
    meter = BoxMeter()
    loop, store, scorer = harness.loop(shape, run_id, generation, MockModel(refuse=True), meter, refuse=True)
    try:
        result = FakeEngine(rounds).run(Task(SEED_CANDIDATE), loop.server(scorer, max_evals=rounds + 1), loop.context())
    except (ReplayLeakError, Exception) as error:
        print(f"[loadtest] replay of {run_id} failed: {type(error).__name__}: {error}")
        _close_quietly(loop, store)
        return False
    _close_quietly(loop, store)
    replayed = (str(result.best_candidate), result.best_score, result.total_evals)
    after = harness.footprint(run_id)
    if after != before:
        print(f"[loadtest] replay of {run_id} stored new data: {before} -> {after}")
    return replayed == expected and meter.opened == 0 and after == before


def _vercel_usd(vcpus: int, wall_seconds: float, active_seconds: float) -> Decimal:
    """Price one Vercel box from its lifetime and active CPU time, with the code base's own price function.

    Args:
        vcpus: The box size.
        wall_seconds: How long the box was open.
        active_seconds: How long its CPU was busy.

    Returns:
        The box's list price in USD.
    """
    session = {
        "id": "loadtest",
        "status": "stopped",
        "region": VERCEL_REGION,
        "vcpus": vcpus,
        "memory": vcpus * 2048,
        "startedAt": 0,
        "stoppedAt": max(int(wall_seconds * 1000), 0),
        "activeCpuDurationMs": max(int(active_seconds * 1000), 0),
        "networkTransfer": {"ingress": 0, "egress": 0},
    }
    return vercel_actual_usd(session, session_id="loadtest", vcpus=vcpus)


def _modal_usd(gpu: str, vcpus: int, seconds: float) -> Decimal:
    """Price one Modal GPU box from its lifetime at list prices; memory is not priced.

    Args:
        gpu: The GPU type.
        vcpus: The CPU cores.
        seconds: How long the box was open.

    Returns:
        The box's list price in USD.
    """
    rate = MODAL_GPU_USD_PER_SECOND[gpu] + vcpus * MODAL_CPU_CORE_USD_PER_SECOND
    return rate * Decimal(str(seconds))


def sandbox_cost(shape: Shape, outcome: RunOutcome, model_wait_seconds: float) -> dict[str, Any]:
    """Compute one run's sandbox price from metered box-seconds: the new path against the old path.

    The new path opens a fresh box for each step, and each box is busy for its whole life. The old path ran
    the engine in one box for the whole run, so that box was open for the step work and for every model
    wait, and busy only for the step work.

    Args:
        shape: The shape.
        outcome: The run, with its meter.
        model_wait_seconds: The assumed wait of one real model call.

    Returns:
        Box-seconds and USD for both paths, and the ratio.
    """
    busy = outcome.meter.box_seconds
    old_wall = busy + outcome.model_calls * model_wait_seconds
    if shape.gpu:
        new_usd = sum((_modal_usd(shape.gpu, shape.vcpus, seconds) for seconds in outcome.meter.lifetimes), Decimal(0))
        old_usd = _modal_usd(shape.gpu, shape.vcpus, old_wall)
        provider = f"modal gpu:{shape.gpu} (assumed list price, MODAL_GPU_USD_PER_SECOND)"
    else:
        new_usd = sum(
            (_vercel_usd(shape.vcpus, seconds, seconds) for seconds in outcome.meter.lifetimes),
            Decimal(0),
        )
        old_usd = _vercel_usd(shape.vcpus, old_wall, busy)
        provider = f"vercel {VERCEL_REGION} (list price)"
    return {
        "provider": provider,
        "boxes_new": outcome.meter.opened,
        "box_seconds_new": round(busy, 3),
        "box_seconds_old": round(old_wall, 3),
        "usd_new": float(round(new_usd, 6)),
        "usd_old": float(round(old_usd, 6)),
        "new_over_old": float(round(new_usd / old_usd, 3)) if old_usd else None,
    }


def outcome_report(shape: Shape, outcome: RunOutcome, model_wait_seconds: float) -> dict[str, Any]:
    """Return the report row of one run.

    Args:
        shape: The shape.
        outcome: The run.
        model_wait_seconds: The assumed wait of one real model call.

    Returns:
        The JSON row.
    """
    row: dict[str, Any] = {
        "run_id": outcome.run_id,
        "ok": outcome.ok,
        "finished": outcome.finished,
        "gapless": outcome.gapless,
        "replayed": outcome.replayed,
        "result": list(outcome.result) if outcome.result else None,
        "restarts": outcome.restarts,
        "model_calls": outcome.model_calls,
        "steps": outcome.steps,
        "boxes_open_max": outcome.meter.open_max,
        "wall_seconds": round(outcome.wall_seconds, 3),
        "sandbox": sandbox_cost(shape, outcome, model_wait_seconds),
    }
    if shape.gpu:
        row["gpu_placements"] = dict(outcome.meter.placements)
        row["gpu_placement_checked"] = sum(outcome.meter.placements.values()) == outcome.meter.opened
        row["ok"] = row["ok"] and row["gpu_placement_checked"]
    if outcome.error:
        row["error"] = outcome.error
    return row


@dataclass
class ReferenceShapesConfig:
    """Knob set for one reference-shapes run.

    Attributes:
        rounds: The fake engine's model rounds for each shape.
        model_wait_seconds: The assumed wait of one real model call, for the old-path price.
    """

    rounds: int
    model_wait_seconds: float


def run(config: ReferenceShapesConfig) -> ScenarioResult:
    """Run each shape once with no chaos, check its step log and price its sandbox use.

    Args:
        config: The knobs.

    Returns:
        The scenario result; ``slo_passed`` is ``True`` only when every shape is correct and replays.
    """
    runtime, provider = box_runtime()
    metrics = ScenarioMetrics("reference_shapes")
    rows: dict[str, Any] = {}
    with tempfile.TemporaryDirectory(prefix="sky727-shapes-") as folder:
        harness = Harness(Path(folder), runtime)
        try:
            for index, shape in enumerate(SHAPES):
                outcome = run_shape(harness, shape, f"shape-{index}", rounds=config.rounds)
                metrics.record(status_code=200 if outcome.ok else 500, latency_seconds=outcome.wall_seconds)
                rows[shape.name] = outcome_report(shape, outcome, config.model_wait_seconds)
        finally:
            harness.close()
    result = metrics.finish()
    result.extras = {
        "box_provider": provider,
        "gpu_provider": "fake-modal (placement check, local box)",
        "model": "in-process mock",
        "rounds": config.rounds,
        "assumed_model_wait_seconds": config.model_wait_seconds,
        "cost_note": (
            "Vercel bills memory in whole minutes for each box, so a box for each short step can cost more than"
            " one long box; Modal bills each second, so the new path saves the model waits."
        ),
        "shapes": rows,
    }
    result.slo_violations = [
        f"{name} did not finish correct and replayable" for name, row in rows.items() if not row["ok"]
    ]
    result.slo_passed = not result.slo_violations
    return result


def default_config() -> ReferenceShapesConfig:
    """Return the knob set from the environment.

    Returns:
        The config: ``LOAD_TEST_SHAPE_ROUNDS`` (default 2) and ``LOAD_TEST_MODEL_WAIT_SECONDS`` (default 20).
    """
    return ReferenceShapesConfig(
        rounds=int(os.environ.get("LOAD_TEST_SHAPE_ROUNDS", "2")),
        model_wait_seconds=float(os.environ.get("LOAD_TEST_MODEL_WAIT_SECONDS", str(DEFAULT_MODEL_WAIT_SECONDS))),
    )


async def main(api_base_url: str, mock_lm_url: str) -> ScenarioResult:
    """Run the scenario in a thread and print the console report.

    Args:
        api_base_url: Not used: the scenario needs no stack.
        mock_lm_url: Not used: the model is in-process.

    Returns:
        The scenario result.
    """
    result = await asyncio.to_thread(run, default_config())
    print_result(result)
    return result


if __name__ == "__main__":
    asyncio.run(main("", ""))
