"""10K-agent swarm: step log commit rate and step wait, with no paid call (SKY-727).

The swarm drives the step log (``core/storage/step_log_store.py``) the way
the engine loops of many agents do. Each agent commits a step and then
waits until the step is durable, as ``EngineLoop`` and the tree sandbox do
after every step. An even step is a model step with a mock reply, and an
odd step is a command step. A command step opens a box on an in-process
fake box provider, puts the new tree object, closes the box and commits the
step with the tree id. About 100 agents share one run (one ``jobs`` row).

Each step-log shard runs in its own OS process, with its own database and
its own step log worker, as one Cloudflare Durable Object owns its own
storage. Run ``i`` goes to shard ``i % shards``. The shard processes start
their agents together at a barrier. In each shard process, at most
``MAX_DRIVERS_PER_SHARD`` driver threads serve the agents, whatever the
agent count. Each agent is due to commit one step every ``step_interval``
seconds, so the agents ask for ``TARGET_COMMITS_PER_SECOND`` times
``OFFERED_LOAD_MARGIN`` steps per second. But each agent keeps only one
step in flight, so the swarm offers at most the agent count divided by the
step wait. The step wait is about one flush window or more (0.5 s by
default), so fewer than about 3,000 agents cannot offer the target, and
the default 1,000 agents are a smoke run that fails the rate gate. The
gate needs about 10,000 agents. A real agent also waits for its model and
its box between steps. Each agent starts at a seeded random offset in its
first interval, so the agents do not commit in lock step. A step that is
ready while the agent's last step is not durable waits for it, as each
agent's engine loop waits only for its own steps. A driver commits each
ready step at once and does not wait for one agent's step to commit
another agent's step.

The scenario measures, across all shard processes on one system-wide
monotonic clock:

- the steady commit rate: the steps that become durable while every agent
  runs, divided by the length of that time. It starts when the first step
  of every agent in every shard is durable: before that, the agents are
  still starting at their offsets. It ends when the first agent of any
  shard finishes its last step: after that, fewer agents commit, as only a
  finite test drains. The agents offer the target rate and a margin, so
  the rate falls below the target when the step log cannot keep up. The
  report also gives the rate up to the last durable step, with the drain;
- the agent step wait: the time from the start of ``commit`` to the moment
  the driver sees the step durable, for each step of every shard. That is
  the time that the step log adds to each agent step. The pause between
  steps is not in it. How late a driver starts the ``commit`` of a ready
  step is reported on its own;
- the box load that the swarm puts on Vercel: box starts per minute and
  concurrent boxes, for a fresh box per step and for one pooled box per
  agent, against the Vercel caps. These figures are only reported, never
  gated (``box_caps_gated`` is ``False``): box caps are out of scope here,
  and SKY-732 owns them.

The swarm runs twice: on 1 shard process, then on ``shards`` shard
processes. Both passes must give the same step log. With fewer than 2
shards, or fewer runs than shards, the passes are not compared and the gate
fails.

The gate passes when the sharded pass commits at least
``TARGET_COMMITS_PER_SECOND`` steps per second in the steady part, the
steady part is at least ``steady_floor_seconds`` long, and its
step wait p99 is at most ``MAX_STEP_WAIT_P99_WINDOWS`` flush windows: one
window for the batch to close, and one for the pack write and the database
commit. The sharded layout is the design, so the 1-shard pass is the
reference for the step log and its rate and wait are only reported. Real
steps also wait for models and boxes, so a real swarm commits fewer steps
per second. The gate shows only that the step log is not the limit at this
agent count; it does not measure the models or the boxes.

The database is one SQLite file per shard, unless ``LOAD_TEST_SWARM_DB_URL``
names a throwaway Postgres on ``localhost``, ``127.0.0.1`` or ``::1``; there
all shards share the database. The
objects go to a local folder, unless ``LOAD_TEST_SWARM_S3_BUCKET`` names a
bucket on a local S3 server such as MinIO; then ``LOAD_TEST_SWARM_S3_ENDPOINT``
must name that server on ``localhost``, ``127.0.0.1`` or ``::1``, and the
swarm deletes its packs at the end. The flush window and the size cap come
from the code defaults, not from ``settings``, so ``backend/.env`` does not
change them. The parent process creates the job rows before the shard
processes start, and reads the step log back after they stop.

The swarm does not run the actor tree: ``Agent.step`` reads the budget and
settles the ledger on each step, which tests the ledger and not the step
log. Nothing here spends money.
"""

from __future__ import annotations

import asyncio
import hashlib
import heapq
import ipaddress
import json
import math
import multiprocessing
import os
import queue
import random
import statistics
import tempfile
import threading
import time
import traceback
from collections import deque
from concurrent.futures import ThreadPoolExecutor
from dataclasses import dataclass
from pathlib import Path
from typing import Any
from urllib.parse import urlsplit

from sqlalchemy import create_engine, delete, make_url, select, text
from sqlalchemy.engine import Engine
from sqlalchemy.orm import Session

# ``python -m load_tests.scenarios.swarm_10k`` does not pass through run_all: litellm, which ``core`` can
# import, fetches its price map over HTTPS at import unless this is set. It must come before the core imports.
os.environ.setdefault("LITELLM_LOCAL_MODEL_COST_MAP", "True")

from core.config import Settings
from core.storage.models import AgentStepModel, Base, JobModel, StoragePackModel, TreeObjectModel
from core.storage.object_store import LocalObjectStore, S3ObjectStore
from core.storage.step_log_store import StepLogStore

from ..lib.metrics import ScenarioMetrics, ScenarioResult
from ..lib.reporter import print_result

try:
    import boto3
except ImportError:
    boto3 = None

AGENTS_PER_RUN = 100
GRANT_LIST_ID = "grants-swarm"
RUN_PREFIX = "swarm-"
TARGET_COMMITS_PER_SECOND = 5_700
# The swarm offers more than the target: a swarm that offers exactly the target measures a little under it
# about half the time, and a pass must prove that the step log sustains at least the target.
OFFERED_LOAD_MARGIN = 1.05
# One window for the batch to close, and one more for the pack write and the database commit.
MAX_STEP_WAIT_P99_WINDOWS = 2
DURABLE_TIMEOUT_SECONDS = 300
# The code defaults, not ``settings``: the swarm must not take its flush knobs from ``backend/.env``.
DEFAULT_FLUSH_WINDOW_SECONDS = Settings.model_fields["step_log_flush_window_seconds"].default
DEFAULT_FLUSH_MAX_BYTES = Settings.model_fields["step_log_flush_max_bytes"].default
# A reference shape run (reference_shapes.py, 2 rounds) commits 8 steps and opens 6 boxes: the seed's
# command and metric, and a command and a metric after each of the 2 model steps.
REFERENCE_BOX_STEPS_PER_STEP = 6 / 8
LOCAL_S3_HOSTS = frozenset({"localhost", "127.0.0.1", "::1"})
# The only libpq query keys the swarm database URL may carry: none of them can change the host.
DB_QUERY_KEYS = frozenset({"sslmode", "connect_timeout", "application_name"})
# Vercel's sandbox caps for one team: vCPU box starts per minute and concurrent vCPUs.
VERCEL_VCPU_STARTS_PER_MINUTE = 5_000
VERCEL_CONCURRENT_VCPUS = 10_000
# The step log writes a batch as one multi-row INSERT. SQLite allows 32,766 bind variables in one statement,
# about 2,700 steps, and psycopg2 binds on the client with no such cap. A small batch keeps SQLite under it.
SQLITE_FLUSH_MAX_BYTES = 32 * 1024
# How long a shard process waits at the start barrier, and the parent for all of them.
BARRIER_TIMEOUT_SECONDS = 300
# One OS thread for each agent crashed a Mac at 10K agents (a kernel panic at about 9,000 threads in one
# process), so a fixed number of driver threads serves all the agents of one shard process.
MAX_DRIVERS_PER_SHARD = 32
# Each shard is an OS process with its own drivers and flusher, so the shard count has a cap too.
MAX_SHARDS = 32
# One shard's single flusher commits about 2,000 steps per second on SQLite, and each agent keeps one step in
# flight, so the step wait is about agents per shard / 2,000 s. At 800 agents per shard the measured step
# wait p99 was 763 ms, under the 1,000 ms limit; at 1,250 it was 1,100 to 1,240 ms. More single-writer shards
# scale out, as more Durable Objects do.
AGENTS_PER_SHARD = 800
PASS_TIMEOUT_SECONDS = 3600
# How often the parent checks for a shard process that died with no report.
REPORT_POLL_SECONDS = 1.0
# With 2 steps for each agent the steady part can be close to 0 s, and a few steps over it give any rate.
MIN_STEPS_PER_AGENT = 5
# The steady part must hold several flushes, so one batch at its edges does not decide the rate, and more
# than one step of each agent.
MIN_STEADY_FLUSH_WINDOWS = 4
MIN_STEADY_STEP_INTERVALS = 2


@dataclass
class SwarmConfig:
    """Knob set for one swarm run.

    Attributes:
        agents: How many agents.
        steps_per_agent: How many steps each agent commits; at least ``MIN_STEPS_PER_AGENT``, for the steady rate.
        shards: The step-log shards of the second pass; below 2 the passes are not compared.
        flush_window_seconds: The step log's buffer window.
        box_seconds: The assumed life of one real box, for the box load figures.
        box_vcpus: The size of one box.
        box_steps_per_step: The boxes that one step opens on average, for the box load figures.
        db_url: A throwaway Postgres URL, or ``None`` for SQLite files.
    """

    agents: int
    steps_per_agent: int
    shards: int
    flush_window_seconds: float
    box_seconds: float
    box_vcpus: int
    box_steps_per_step: float
    db_url: str | None


class FakeBoxProvider:
    """An in-process box provider: it counts the boxes and keeps nothing."""

    def __init__(self) -> None:
        """Start with no box."""
        self._lock = threading.Lock()
        self.opened = 0
        self.open_now = 0
        self.open_max = 0

    def open(self) -> None:
        """Open one box."""
        with self._lock:
            self.opened += 1
            self.open_now += 1
            self.open_max = max(self.open_max, self.open_now)

    def close(self) -> None:
        """Close one box."""
        with self._lock:
            self.open_now -= 1


@dataclass
class Shard:
    """One step-log shard, as the parent process sees it: its database, its object store and its worker id.

    Attributes:
        url: The database URL.
        folder: The shard's private folder; its objects go to ``folder / "objects"``.
        engine: The parent's connection to the database.
        objects: The parent's object store.
        s3: The S3 client of a local S3 bucket, or ``None`` for a local folder.
        runs: The shard's job ids.
        worker_id: The shard's step log worker id.
    """

    url: str
    folder: Path
    engine: Engine
    objects: Any
    s3: Any
    runs: list[str]
    worker_id: str


def run_id_for(index: int) -> str:
    """Return the id of one run.

    Args:
        index: The run index.

    Returns:
        The job id.
    """
    return f"{RUN_PREFIX}{index:06d}"


def _object_store(folder: Path) -> tuple[Any, Any]:
    """Return the object store: a bucket on a local S3 server when one is set, else a local folder.

    Args:
        folder: The local folder.

    Returns:
        The object store, and its S3 client or ``None``.

    Raises:
        RuntimeError: When a bucket is set with no local endpoint, or boto3 is not installed.
    """
    bucket = os.environ.get("LOAD_TEST_SWARM_S3_BUCKET", "").strip()
    if not bucket:
        return LocalObjectStore(folder), None
    # With no endpoint, boto3 goes to real AWS S3; the swarm writes only to a local server.
    endpoint = os.environ.get("LOAD_TEST_SWARM_S3_ENDPOINT", "").strip()
    if urlsplit(endpoint).hostname not in LOCAL_S3_HOSTS:
        raise RuntimeError(
            f"LOAD_TEST_SWARM_S3_BUCKET needs LOAD_TEST_SWARM_S3_ENDPOINT on {sorted(LOCAL_S3_HOSTS)}, got {endpoint!r}"
        )
    if boto3 is None:
        raise RuntimeError("boto3 is not installed")
    client = boto3.client("s3", endpoint_url=endpoint)
    return S3ObjectStore(bucket, client), client


def _check_db_url(url: str | None) -> None:
    """Refuse a database URL that is not on the local host.

    The swarm creates tables and deletes the swarm's rows in the database that the URL names.

    Args:
        url: ``LOAD_TEST_SWARM_DB_URL``, or ``None`` for SQLite files.

    Raises:
        RuntimeError: When the URL names a host that is not local, or no host; when it has a query key off the
            allowed list; or when a libpq environment variable can send the connection to another host.
    """
    if url is None:
        return
    parsed = make_url(url)
    # libpq takes the host from query keys (host, hostaddr, service, a nested dsn) before the URL host, so a
    # local URL host proves nothing unless every other query key is refused.
    unknown = {key.lower() for key in parsed.query} - DB_QUERY_KEYS
    if unknown or parsed.host not in LOCAL_S3_HOSTS:
        raise RuntimeError(f"LOAD_TEST_SWARM_DB_URL must name a database on {sorted(LOCAL_S3_HOSTS)}")
    # PGHOSTADDR overrides the URL host, and a service file can set hostaddr; PGHOST is safe only when local.
    redirects = [
        name
        for name in ("PGHOST", "PGHOSTADDR", "PGSERVICE", "PGSERVICEFILE")
        if os.environ.get(name, "").strip() and not (name == "PGHOST" and os.environ[name].strip() in LOCAL_S3_HOSTS)
    ]
    if redirects:
        raise RuntimeError(f"LOAD_TEST_SWARM_DB_URL is refused while {redirects} are set: unset them")


def _check_server_addr(engine: Engine) -> None:
    """Refuse a Postgres server that does not answer on a loopback address.

    This is a second guard after ``_check_db_url``: it asks the server where the connection really went.

    Args:
        engine: The Postgres database.

    Raises:
        RuntimeError: When the server address is not loopback.
    """
    with engine.connect() as connection:
        address = connection.execute(text("SELECT inet_server_addr()")).scalar()
    # NULL means a Unix socket, which is local by definition.
    if address is not None and not ipaddress.ip_address(str(address).split("/")[0]).is_loopback:
        raise RuntimeError(f"LOAD_TEST_SWARM_DB_URL reached server address {address}, which is not loopback")


def _delete_packs(shard: Shard) -> None:
    """Delete the shard's packs from a local S3 bucket; a local folder goes with its temporary folder.

    Args:
        shard: The shard.
    """
    if shard.s3 is None:
        return
    keys = shard.objects.list(f"packs/{shard.worker_id}/")
    for start in range(0, len(keys), 1000):
        shard.s3.delete_objects(
            Bucket=os.environ["LOAD_TEST_SWARM_S3_BUCKET"].strip(),
            Delete={"Objects": [{"Key": key} for key in keys[start : start + 1000]], "Quiet": True},
        )


def _swarm_tables() -> list[Any]:
    """Return the step-log tables and every table that their foreign keys reach.

    A plain Postgres has no pgvector, so the whole schema does not go in; these tables need none.

    Returns:
        The tables, in dependency order.
    """
    needed = {"jobs", "agent_steps", "tree_objects", "storage_packs"}
    pending = list(needed)
    while pending:
        for key in Base.metadata.tables[pending.pop()].foreign_keys:
            name = key.column.table.name
            if name not in needed:
                needed.add(name)
                pending.append(name)
    return [table for table in Base.metadata.sorted_tables if table.name in needed]


def _reset(engine: Engine, runs: list[str]) -> None:
    """Create the missing swarm tables and put one fresh running job row in for each run.

    Only rows of the swarm's own runs and workers go, so a shared database keeps its other rows.

    Args:
        engine: The database.
        runs: The job ids.
    """
    Base.metadata.create_all(engine, tables=_swarm_tables())
    with Session(engine) as session:
        session.execute(delete(AgentStepModel).where(AgentStepModel.run_id.startswith(RUN_PREFIX)))
        session.execute(delete(TreeObjectModel).where(TreeObjectModel.username.startswith(RUN_PREFIX)))
        session.execute(delete(StoragePackModel).where(StoragePackModel.worker_id.startswith(RUN_PREFIX)))
        session.execute(delete(JobModel).where(JobModel.optimization_id.startswith(RUN_PREFIX)))
        session.add_all(
            JobModel(optimization_id=run_id, username=run_id, status="running", execution_generation=1)
            for run_id in runs
        )
        session.commit()


def _now() -> float:
    """Return the system-wide monotonic clock, which all shard processes share.

    Returns:
        The clock in seconds.
    """
    return time.clock_gettime(time.CLOCK_MONOTONIC)


def _commit_step(
    run_id: str, agent_id: str, seq: int, store: StepLogStore, boxes: FakeBoxProvider
) -> tuple[str, float]:
    """Commit one step of one agent; a command step first opens a box and puts its tree object.

    Args:
        run_id: The agent's job id.
        agent_id: The agent id.
        seq: The step's seq.
        store: The shard's step log.
        boxes: The fake box provider.

    Returns:
        The step id and the start of ``commit``.
    """
    tree_id = None
    if seq % 2:
        boxes.open()
        tree = f"{run_id}/{agent_id}/{seq}".encode()
        tree_id = hashlib.sha256(tree).hexdigest()
        store.put_object(run_id, tree_id, tree)
        boxes.close()
        body = {"command": f"sh work.sh {seq}", "exit_code": 0, "stdout": f"{seq}\n"}
    else:
        body = {"prompt": f"improve {seq}", "response": str(seq + 1), "cost_cents": "0.25"}
    started = _now()
    step_id = store.commit(
        run_id=run_id,
        agent_id=agent_id,
        seq=seq,
        kind="command" if tree_id else "model",
        execution_generation=1,
        grant_list_id=GRANT_LIST_ID,
        body=body,
        tree_id=tree_id,
    )
    return step_id, started


def driver_count(agent_count: int) -> int:
    """Return how many driver threads one shard process starts for its agents.

    Args:
        agent_count: The shard's agents.

    Returns:
        At least 1 and at most ``MAX_DRIVERS_PER_SHARD``, whatever the agent count.
    """
    return max(1, min(MAX_DRIVERS_PER_SHARD, agent_count))


def default_shards(agent_count: int) -> int:
    """Return the default shard count for an agent count: one shard for each ``AGENTS_PER_SHARD`` agents.

    Args:
        agent_count: The swarm's agents.

    Returns:
        At least 2, so the passes are compared, and at most ``MAX_SHARDS``.
    """
    return max(2, min(MAX_SHARDS, math.ceil(agent_count / AGENTS_PER_SHARD)))


def step_interval(agent_count: int) -> float:
    """Return the seconds between the steps of one agent, so the whole swarm offers the target rate and a margin.

    Args:
        agent_count: The swarm's agents.

    Returns:
        ``agent_count / (TARGET_COMMITS_PER_SECOND * OFFERED_LOAD_MARGIN)``.
    """
    return agent_count / (TARGET_COMMITS_PER_SECOND * OFFERED_LOAD_MARGIN)


def steady_floor_seconds(config: SwarmConfig) -> float:
    """Return the shortest steady part that the gate accepts.

    Args:
        config: The knobs.

    Returns:
        ``MIN_STEADY_FLUSH_WINDOWS`` flush windows or ``MIN_STEADY_STEP_INTERVALS`` step intervals, whichever is
        longer.
    """
    return max(
        MIN_STEADY_FLUSH_WINDOWS * config.flush_window_seconds,
        MIN_STEADY_STEP_INTERVALS * step_interval(config.agents),
    )


def start_offset(run_id: str, agent_id: str, interval: float) -> float:
    """Return one agent's start offset: a seeded random point in its first step interval.

    The seed is the agent's own id, so the offset is the same on every shard layout and in every run.

    Args:
        run_id: The agent's job id.
        agent_id: The agent id.
        interval: The seconds between the agent's steps.

    Returns:
        The offset in seconds, from 0 up to ``interval``.
    """
    return random.Random(f"{run_id}/{agent_id}").random() * interval


def _waiter(store: StepLogStore, step_id: str) -> Any:
    """Return the durable barrier of a buffered step, or ``None`` when it is already flushed.

    Harness only: this reads private fields of the step log, which has no public way to wait for one
    step's barrier without a database read. ``durable`` waits on this barrier, but a call after the flush
    finds no barrier and reads the database for the step. The engine loop calls ``durable`` right after
    ``commit`` and never takes that path. A driver that serves many agents checks its steps later, so
    ``durable`` would add one database read for most steps, a load that no real agent puts on the store.

    Args:
        store: The shard's step log.
        step_id: The id that ``commit`` returned.

    Returns:
        The barrier, or ``None``.
    """
    with store._cond:
        return store._waiters.get((step_id, 1))


def _drive_or_stop(
    agents: list[tuple[str, str]],
    config: SwarmConfig,
    store: StepLogStore,
    boxes: FakeBoxProvider,
    origin: float,
    stop: threading.Event,
) -> list[list[tuple[float, float, float]]]:
    """Run ``_drive`` and set ``stop`` when it fails, so the shard's other drivers return soon.

    ``pool.map`` gives the results in submit order, so without this a failed driver would wait for every
    earlier driver to finish all its steps before the shard sees the error.

    Args:
        agents: ``(run_id, agent_id)`` of each agent of the group.
        config: The knobs.
        store: The shard's step log.
        boxes: The fake box provider.
        origin: The shared start on the system-wide clock.
        stop: The shard's stop, shared by all its drivers.

    Returns:
        What ``_drive`` returns.
    """
    try:
        return _drive(agents, config, store, boxes, origin, stop)
    except BaseException:
        stop.set()
        raise


def _drive(
    agents: list[tuple[str, str]],
    config: SwarmConfig,
    store: StepLogStore,
    boxes: FakeBoxProvider,
    origin: float,
    stop: threading.Event,
) -> list[list[tuple[float, float, float]]]:
    """Run every step of a group of agents on one thread, each agent at the target pace.

    Each agent's step ``k`` is ready at ``origin`` plus its start offset plus ``k`` step intervals, or when
    its last step is durable, if that is later. The thread commits each step when it is ready and does
    not wait for one agent's step to commit another agent's step. It takes the durable steps in commit
    order: the step log flushes its buffer in that order, so no step is durable before an earlier one.

    Args:
        agents: ``(run_id, agent_id)`` of each agent of the group.
        config: The knobs.
        store: The shard's step log.
        boxes: The fake box provider.
        origin: The shared start on the system-wide clock.
        stop: Set when a driver of the shard fails or the shard gets Ctrl-C; the thread then returns at its
            next turn.

    Returns:
        For each agent, and for each seq, the moment the step was ready, the start of ``commit`` and the
        moment the thread saw the step durable; only the steps up to a stop.

    Raises:
        RuntimeError: When a step failed the fence, or the step log closed before the step was durable.
        TimeoutError: When a step is not durable in ``DURABLE_TIMEOUT_SECONDS``.
    """
    interval = step_interval(config.agents)
    steps: list[list[tuple[float, float, float]]] = [[] for _ in agents]
    starts = [origin + start_offset(*agent, interval) for agent in agents]
    due = [(start, index, 0) for index, start in enumerate(starts)]
    heapq.heapify(due)
    in_flight: deque[tuple[int, int, str, float, float, Any]] = deque()
    while (due or in_flight) and not stop.is_set():
        while due and due[0][0] <= _now():
            ready, index, seq = heapq.heappop(due)
            step_id, started = _commit_step(*agents[index], seq, store, boxes)
            in_flight.append((index, seq, step_id, ready, started, _waiter(store, step_id)))
        while in_flight:
            index, seq, step_id, ready, started, waiter = in_flight[0]
            if waiter is None:
                store.durable(step_id, 1, timeout=DURABLE_TIMEOUT_SECONDS)
            elif not waiter.is_set():
                if _now() - started > DURABLE_TIMEOUT_SECONDS:
                    raise TimeoutError(f"step {step_id} is not durable yet")
                break
            elif waiter.closed or waiter.failure is not None:
                raise RuntimeError(f"step {step_id} did not land")
            in_flight.popleft()
            finished = _now()
            steps[index].append((ready, started, finished))
            if seq + 1 < config.steps_per_agent:
                # The pace keeps to the agent's own schedule: a late step does not push back the later ones.
                heapq.heappush(due, (max(starts[index] + (seq + 1) * interval, finished), index, seq + 1))
        # Each wait ends within 1 s, so the thread sees a stop soon also with a long step interval.
        if in_flight:
            # Wake for the next ready step even when the oldest step is not durable yet.
            in_flight[0][5].wait(min(1.0, max(0.0, due[0][0] - _now())) if due else 1.0)
        elif due:
            stop.wait(min(1.0, max(0.0, due[0][0] - _now())))
    return steps


def _digest(shards: list[Shard]) -> tuple[str, int]:
    """Hash the whole step log: every row's fields and the hash of its body, read back from the packs.

    The pack keys and offsets differ between shard layouts, so they are not in the hash.

    Args:
        shards: The shards.

    Returns:
        The hex digest and the step count.
    """
    records: list[str] = []
    for shard in shards:
        packs: dict[str, bytes] = {}
        with Session(shard.engine) as session:
            # Shards can share one database, and each shard reads only its own runs from its own packs.
            steps = session.execute(select(AgentStepModel).where(AgentStepModel.run_id.in_(shard.runs))).scalars().all()
            objects = (
                session.execute(select(TreeObjectModel).where(TreeObjectModel.username.in_(shard.runs))).scalars().all()
            )
            for row in [*steps, *objects]:
                if row.pack_key not in packs:
                    packs[row.pack_key] = shard.objects.get(row.pack_key)
            for step in steps:
                body = packs[step.pack_key][step.pack_offset : step.pack_offset + step.pack_length]
                records.append(
                    json.dumps(
                        [
                            "step",
                            step.step_id,
                            step.run_id,
                            step.agent_id,
                            step.seq,
                            step.kind,
                            step.execution_generation,
                            step.grant_list_id,
                            step.tree_id,
                            hashlib.sha256(body).hexdigest(),
                        ]
                    )
                )
            for item in objects:
                data = packs[item.pack_key][item.pack_offset : item.pack_offset + item.pack_length]
                records.append(json.dumps(["object", item.username, item.object_id, hashlib.sha256(data).hexdigest()]))
    records.sort()
    return hashlib.sha256("\n".join(records).encode()).hexdigest(), sum(1 for r in records if r.startswith('["step"'))


def _percentile_ms(values: list[float], fraction: float) -> float:
    """Return one percentile of a list of seconds, in milliseconds.

    Args:
        values: The seconds.
        fraction: The percentile, from 0 to 1.

    Returns:
        The percentile in milliseconds, or 0 for an empty list.
    """
    if not values:
        return 0.0
    ordered = sorted(values)
    return 1000 * ordered[min(len(ordered) - 1, math.ceil(fraction * len(ordered)) - 1)]


def _shard_process(
    config: SwarmConfig,
    shard: dict[str, Any],
    agents: list[tuple[str, str]],
    barrier: Any,
    results: Any,
) -> None:
    """Run one shard's agents in this OS process, with its own step log worker, and send back the figures.

    Args:
        config: The knobs.
        shard: The shard's ``url``, ``folder``, ``worker_id`` and ``flush_max_bytes``.
        agents: ``(run_id, agent_id)`` of each agent of the shard.
        barrier: The start barrier that all shard processes share.
        results: The queue for the figures, or for the error.
    """
    try:
        url = shard["url"]
        engine = create_engine(url, connect_args={"timeout": 60} if url.startswith("sqlite") else {})
        objects, _ = _object_store(Path(shard["folder"]) / "objects")
        store = StepLogStore(
            engine,
            objects,
            shard["worker_id"],
            flush_window_seconds=config.flush_window_seconds,
            flush_max_bytes=shard["flush_max_bytes"],
        )
        boxes = FakeBoxProvider()
        barrier.wait(timeout=BARRIER_TIMEOUT_SECONDS)
        started = _now()
        stop = threading.Event()
        try:
            drivers = driver_count(len(agents))
            with ThreadPoolExecutor(max_workers=drivers, thread_name_prefix="swarm-driver") as pool:
                try:
                    groups = pool.map(
                        lambda group: _drive_or_stop(group, config, store, boxes, started, stop),
                        [agents[i::drivers] for i in range(drivers)],
                    )
                    done = [steps for group in groups for steps in group]
                except BaseException:
                    # The pool's exit waits for every driver, so the drivers must stop first on Ctrl-C.
                    stop.set()
                    raise
        finally:
            store.close()
            engine.dispose()
        results.put(
            {
                "worker_id": shard["worker_id"],
                "started": started,
                "waits": [finished - begun for steps in done for _, begun, finished in steps],
                "lates": [begun - ready for steps in done for ready, begun, _ in steps],
                "times": [finished for steps in done for _, _, finished in steps],
                "first_round_durable": max(steps[0][2] for steps in done),
                "first_agent_done": min(steps[-1][2] for steps in done),
                "boxes_opened": boxes.opened,
                "boxes_open_max": boxes.open_max,
            }
        )
    except BaseException:
        # A shard that fails before the barrier must not leave the others waiting there.
        barrier.abort()
        results.put({"worker_id": shard["worker_id"], "error": traceback.format_exc()})


def collect_reports(results: Any, processes: dict[str, Any], timeout: float) -> list[dict[str, Any]]:
    """Take one report from each shard process, and fail soon when a process exits with no report.

    A process puts its report before it exits, so a report can still be in the queue when the process is
    gone. A process counts as lost only when it is still gone with no report at the next poll.

    Args:
        results: The queue that the shard processes put their reports on.
        processes: Each shard process by its worker id.
        timeout: The most seconds to wait for all the reports.

    Returns:
        The reports.

    Raises:
        RuntimeError: When a process exits with no report, or the reports do not all come in time.
    """
    deadline = time.monotonic() + timeout
    pending = dict(processes)
    reports: list[dict[str, Any]] = []
    suspects: set[str] = set()
    while pending:
        try:
            report = results.get(timeout=REPORT_POLL_SECONDS)
        except queue.Empty:
            gone = {worker_id for worker_id, process in pending.items() if not process.is_alive()}
            lost = sorted(gone & suspects)
            if lost:
                codes = ", ".join(f"{worker_id} (exit code {pending[worker_id].exitcode})" for worker_id in lost)
                raise RuntimeError(f"shard process(es) exited with no report: {codes}") from None
            suspects = gone
            if time.monotonic() > deadline:
                raise RuntimeError(f"{len(pending)} shard process(es) did not report in time") from None
            continue
        pending.pop(report["worker_id"], None)
        reports.append(report)
    return reports


def run_pass(config: SwarmConfig, shard_count: int, folder: Path) -> dict[str, Any]:
    """Run the swarm once over a number of shard processes and measure it.

    Args:
        config: The knobs.
        shard_count: The step-log shards, one OS process each; run ``i`` goes to shard ``i % shard_count``.
        folder: A private, empty folder for the SQLite files and the objects.

    Returns:
        The pass figures, with the step log digest.

    Raises:
        RuntimeError: When the database URL is not local, or a shard process fails or does not report.
    """
    _check_db_url(config.db_url)
    runs = [run_id_for(index) for index in range(math.ceil(config.agents / AGENTS_PER_RUN))]
    agents = [(runs[index // AGENTS_PER_RUN], f"agent-{index:05d}") for index in range(config.agents)]
    stamp = f"{int(time.time() * 1000) % 10**9}"
    shards: list[Shard] = []
    reports: list[dict[str, Any]] = []
    # Spawn, not fork: a forked child would inherit the parent's engines and threads.
    context = multiprocessing.get_context("spawn")
    processes: list[Any] = []
    # A cleanup error must not hide the error that stopped the pass.
    failed = True
    try:
        for index in range(shard_count):
            shard_folder = folder / f"shard-{index}"
            shard_folder.mkdir(parents=True)
            # A Postgres shard shares the server, as the step log's run shards share one database cluster.
            url = config.db_url or f"sqlite:///{shard_folder / 'db.sqlite'}"
            engine = create_engine(url, connect_args={"timeout": 60} if url.startswith("sqlite") else {})
            if config.db_url is not None:
                _check_server_addr(engine)
            if config.db_url is None or index == 0:
                _reset(engine, runs)
            shard_runs = [run for position, run in enumerate(runs) if position % shard_count == index]
            objects, s3 = _object_store(shard_folder / "objects")
            worker_id = f"{RUN_PREFIX}{stamp}-s{shard_count}-{index}"
            shards.append(Shard(url, shard_folder, engine, objects, s3, shard_runs, worker_id))
        if config.db_url is None:
            for shard in shards:
                with Session(shard.engine) as session:
                    session.execute(delete(JobModel).where(JobModel.optimization_id.notin_(shard.runs)))
                    session.commit()
        barrier = context.Barrier(shard_count)
        results = context.Queue()
        for shard in shards:
            spec = {
                "url": shard.url,
                "folder": str(shard.folder),
                "worker_id": shard.worker_id,
                "flush_max_bytes": DEFAULT_FLUSH_MAX_BYTES if config.db_url else SQLITE_FLUSH_MAX_BYTES,
            }
            members = set(shard.runs)
            process = context.Process(
                target=_shard_process,
                args=(config, spec, [agent for agent in agents if agent[0] in members], barrier, results),
                name=f"swarm-{shard.worker_id}",
            )
            process.start()
            processes.append(process)
        reports.extend(
            collect_reports(
                results,
                {shard.worker_id: process for shard, process in zip(shards, processes, strict=True)},
                PASS_TIMEOUT_SECONDS,
            )
        )
        errors = [report["error"] for report in reports if "error" in report]
        if errors:
            raise RuntimeError("a shard process failed:\n" + "\n".join(errors))
        digest, stored = _digest(shards)
        failed = False
    finally:
        for process in processes:
            process.join(timeout=30)
            if process.is_alive():
                process.kill()
                process.join()
        # Each shard gets its cleanup, also after another shard's cleanup fails; the first error wins.
        cleanup_error: Exception | None = None
        for shard in shards:
            try:
                _delete_packs(shard)
            except Exception as error:
                cleanup_error = cleanup_error or error
            finally:
                shard.engine.dispose()
        if cleanup_error is not None and not failed:
            raise cleanup_error
    waits = [wait for report in reports for wait in report["waits"]]
    lates = [late for report in reports for late in report["lates"]]
    times = [moment for report in reports for moment in report["times"]]
    started = min(report["started"] for report in reports)
    # The agents start at their offsets; the steady part starts once the first step of every agent is durable.
    steady_from = max(report["first_round_durable"] for report in reports)
    # Each agent runs freely, so the agents finish at different times; after the first one finishes, fewer agents
    # commit, and that drain is a property of a finite test, not of the step log. A real swarm keeps its agents.
    steady_to = min(report["first_agent_done"] for report in reports)
    last = max(times)
    steady_steps = sum(1 for moment in times if steady_from < moment <= steady_to)
    steady_seconds = steady_to - steady_from
    drain_steps = sum(1 for moment in times if moment > steady_from)
    return {
        "shards": shard_count,
        "runs": len(runs),
        "runs_per_shard": [len(shard.runs) for shard in shards],
        "commits": len(waits),
        "steps_stored": stored,
        "seconds_to_durable": round(last - started, 3),
        "steady_steps": steady_steps,
        "steady_seconds": round(steady_seconds, 3),
        "commits_per_second": round(steady_steps / steady_seconds, 1) if steady_seconds > 0 else 0.0,
        "commits_per_second_with_drain": round(drain_steps / (last - steady_from), 1) if last > steady_from else 0.0,
        "commits_per_second_whole_pass": round(len(waits) / (last - started), 1),
        "step_wait_ms": {
            "mean": round(1000 * statistics.fmean(waits), 2),
            "p50": round(_percentile_ms(waits, 0.50), 2),
            "p99": round(_percentile_ms(waits, 0.99), 2),
            "max": round(_percentile_ms(waits, 1.0), 2),
        },
        # How late the drivers committed the ready steps; not part of the step wait, so shown on its own.
        "commit_late_ms": {
            "p50": round(_percentile_ms(lates, 0.50), 2),
            "p99": round(_percentile_ms(lates, 0.99), 2),
            "max": round(_percentile_ms(lates, 1.0), 2),
        },
        "fake_boxes_opened": sum(report["boxes_opened"] for report in reports),
        "fake_boxes_open_max_in_one_shard": max(report["boxes_open_max"] for report in reports),
        "digest": digest,
    }


def box_load(config: SwarmConfig, commits_per_second: float) -> dict[str, Any]:
    """Compute the Vercel box load that the swarm's command steps make, against the Vercel caps.

    Each step starts ``config.box_steps_per_step`` boxes on average, as the reference shapes do. With a fresh
    box for each box step, Little's law gives the concurrent boxes: start rate times box life. With one
    pooled box for each agent, the swarm starts each box once and keeps one box for each agent open.

    Args:
        config: The knobs.
        commits_per_second: The commit rate to compute for.

    Returns:
        Box starts per minute and concurrent boxes, in boxes and in vCPUs, with the cap checks.
    """
    starts_per_second = commits_per_second * config.box_steps_per_step
    per_step_starts = 60 * starts_per_second
    per_step_concurrent = starts_per_second * config.box_seconds
    figures: dict[str, Any] = {"commits_per_second": round(commits_per_second, 1)}
    for name, starts, concurrent in (
        ("fresh_box_per_step", per_step_starts, per_step_concurrent),
        ("pooled_box_per_agent", None, float(config.agents)),
    ):
        vcpu_starts = None if starts is None else starts * config.box_vcpus
        figures[name] = {
            "box_starts_per_minute": None if starts is None else round(starts),
            "vcpu_starts_per_minute": None if vcpu_starts is None else round(vcpu_starts),
            "concurrent_boxes": round(concurrent),
            "concurrent_vcpus": round(concurrent * config.box_vcpus),
            "starts_within_cap": None if vcpu_starts is None else vcpu_starts <= VERCEL_VCPU_STARTS_PER_MINUTE,
            "concurrency_within_cap": concurrent * config.box_vcpus <= VERCEL_CONCURRENT_VCPUS,
        }
    return figures


def run(config: SwarmConfig) -> ScenarioResult:
    """Run the swarm on 1 shard process, then on ``config.shards`` shard processes, and compare the step logs.

    Args:
        config: The knobs.

    Returns:
        The scenario result; ``slo_passed`` needs, on the sharded pass, the target steady commit rate over a
        steady part of at least ``steady_floor_seconds``, a step wait p99 of at most
        ``MAX_STEP_WAIT_P99_WINDOWS`` flush windows, and the same step log on both layouts.

    Raises:
        ValueError: When there is no agent, an agent commits fewer than ``MIN_STEPS_PER_AGENT`` steps, or the
            shards are more than ``MAX_SHARDS``.
    """
    if config.agents < 1:
        raise ValueError(f"the swarm needs at least 1 agent, got {config.agents}")
    if config.steps_per_agent < MIN_STEPS_PER_AGENT:
        raise ValueError(
            f"the swarm needs at least {MIN_STEPS_PER_AGENT} steps for each agent, got {config.steps_per_agent}"
        )
    if config.shards > MAX_SHARDS:
        raise ValueError(f"the swarm starts at most {MAX_SHARDS} shard processes, got {config.shards}")
    runs = math.ceil(config.agents / AGENTS_PER_RUN)
    not_compared = None
    if config.shards < 2:
        not_compared = f"{config.shards} shard(s): the comparison needs at least 2"
    elif runs < config.shards:
        not_compared = f"{runs} run(s) for {config.shards} shards: each shard needs a run"
    metrics = ScenarioMetrics("swarm_10k")
    passes: list[dict[str, Any]] = []
    with tempfile.TemporaryDirectory(prefix="sky727-swarm-") as folder:
        for shard_count in (1,) if not_compared else (1, config.shards):
            figures = run_pass(config, shard_count, Path(folder) / f"pass-{shard_count}")
            metrics.record(status_code=200, latency_seconds=figures["seconds_to_durable"])
            passes.append(figures)
    if not_compared is None and any(count == 0 for figures in passes for count in figures["runs_per_shard"]):
        not_compared = "a shard got no run"
    same_step_log: bool | str = (
        f"not compared: {not_compared}" if not_compared else len({figures["digest"] for figures in passes}) == 1
    )
    gated = passes[-1]
    max_wait_ms = round(1000 * MAX_STEP_WAIT_P99_WINDOWS * config.flush_window_seconds, 2)
    min_steady_seconds = steady_floor_seconds(config)
    result = metrics.finish()
    result.extras = {
        "agents": config.agents,
        "steps_per_agent": config.steps_per_agent,
        "max_drivers_per_shard": MAX_DRIVERS_PER_SHARD,
        "shard_isolation": "one spawned OS process for each shard",
        "flush_window_seconds": config.flush_window_seconds,
        "database": "postgres (LOAD_TEST_SWARM_DB_URL)"
        if config.db_url
        else f"sqlite file per shard, flush cap {SQLITE_FLUSH_MAX_BYTES} bytes",
        "object_store": "local s3 (LOAD_TEST_SWARM_S3_BUCKET)"
        if os.environ.get("LOAD_TEST_SWARM_S3_BUCKET", "").strip()
        else "local folder",
        "box_provider": "in-process fake",
        "model": "mock reply in the step body",
        "target_commits_per_second": TARGET_COMMITS_PER_SECOND,
        "offered_commits_per_second": round(TARGET_COMMITS_PER_SECOND * OFFERED_LOAD_MARGIN, 1),
        "max_step_wait_p99_ms": max_wait_ms,
        "min_steady_seconds": round(min_steady_seconds, 3),
        "gated_pass_shards": gated["shards"],
        "passes": passes,
        "same_step_log": same_step_log,
        "assumed_box_seconds": config.box_seconds,
        "box_vcpus": config.box_vcpus,
        "box_steps_per_step": config.box_steps_per_step,
        "vercel_caps": {
            "vcpu_starts_per_minute": VERCEL_VCPU_STARTS_PER_MINUTE,
            "concurrent_vcpus": VERCEL_CONCURRENT_VCPUS,
        },
        # Reported only: box caps are out of scope for this gate (SKY-732).
        "box_caps_gated": False,
        "box_load_measured": box_load(config, gated["commits_per_second"]),
        "box_load_target": box_load(config, TARGET_COMMITS_PER_SECOND),
    }
    violations = []
    if gated["commits_per_second"] < TARGET_COMMITS_PER_SECOND:
        violations.append(
            f"{gated['shards']}-shard steady commit rate {gated['commits_per_second']}/s"
            f" is below {TARGET_COMMITS_PER_SECOND}/s"
        )
    if gated["steady_seconds"] < min_steady_seconds:
        violations.append(
            f"{gated['shards']}-shard steady part is {gated['steady_seconds']} s, shorter than"
            f" {round(min_steady_seconds, 3)} s: give each agent more steps"
        )
    if gated["step_wait_ms"]["p99"] > max_wait_ms:
        violations.append(
            f"{gated['shards']}-shard step wait p99 is {gated['step_wait_ms']['p99']} ms, above {max_wait_ms} ms"
        )
    violations.extend(
        f"{figures['shards']}-shard pass stored {figures['steps_stored']} of {figures['commits']}"
        for figures in passes
        if figures["steps_stored"] != figures["commits"]
    )
    if not_compared:
        violations.append(f"the shard layouts were not compared: {not_compared}")
    elif same_step_log is not True:
        violations.append("the shard layouts gave different step logs")
    result.slo_violations = violations
    result.slo_passed = not violations
    return result


def default_config() -> SwarmConfig:
    """Return the knob set from the environment.

    Returns:
        The config: ``LOAD_TEST_SWARM_AGENTS`` (default 1000), ``LOAD_TEST_SWARM_STEPS_PER_AGENT`` (default 10),
        ``LOAD_TEST_SWARM_SHARDS`` (default: one shard for each ``AGENTS_PER_SHARD`` agents, from 2 to ``MAX_SHARDS``),
        ``LOAD_TEST_SWARM_FLUSH_WINDOW_SECONDS`` (default: the code default, 0.5),
        ``LOAD_TEST_SWARM_BOX_SECONDS`` (default 30), ``LOAD_TEST_SWARM_BOX_VCPUS`` (default 2),
        ``LOAD_TEST_SWARM_BOX_STEPS_PER_STEP`` (default 0.75, from the reference shapes) and
        ``LOAD_TEST_SWARM_DB_URL`` (default: SQLite files).
    """
    window = os.environ.get("LOAD_TEST_SWARM_FLUSH_WINDOW_SECONDS", "").strip()
    agents = int(os.environ.get("LOAD_TEST_SWARM_AGENTS", "1000"))
    shards = os.environ.get("LOAD_TEST_SWARM_SHARDS", "").strip()
    return SwarmConfig(
        agents=agents,
        steps_per_agent=int(os.environ.get("LOAD_TEST_SWARM_STEPS_PER_AGENT", "10")),
        shards=int(shards) if shards else default_shards(agents),
        flush_window_seconds=float(window) if window else DEFAULT_FLUSH_WINDOW_SECONDS,
        box_seconds=float(os.environ.get("LOAD_TEST_SWARM_BOX_SECONDS", "30")),
        box_vcpus=int(os.environ.get("LOAD_TEST_SWARM_BOX_VCPUS", "2")),
        box_steps_per_step=float(
            os.environ.get("LOAD_TEST_SWARM_BOX_STEPS_PER_STEP", str(REFERENCE_BOX_STEPS_PER_STEP))
        ),
        db_url=os.environ.get("LOAD_TEST_SWARM_DB_URL", "").strip() or None,
    )


async def main(api_base_url: str, mock_lm_url: str) -> ScenarioResult:
    """Run the swarm in a thread and print the console report.

    Args:
        api_base_url: Not used: the swarm needs no stack.
        mock_lm_url: Not used: the model reply is a mock.

    Returns:
        The scenario result.
    """
    result = await asyncio.to_thread(run, default_config())
    print_result(result)
    return result


if __name__ == "__main__":
    asyncio.run(main("", ""))
