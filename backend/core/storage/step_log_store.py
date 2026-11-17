"""The agent step log: one commit per command and one per model response (SKY-710).

:meth:`StepLogStore.commit` only buffers the step and returns its id, so the
agent does not wait. A flusher thread collects all the worker's buffered
steps and tree objects for the flush window (or until the size cap), then
writes them as one pack object and records them in one Postgres
transaction. :meth:`StepLogStore.durable` is the barrier that outside
effects wait on.

Until its flush commits, a step or tree object is read from the buffer (the
LSM memtable), so the next box can mount a tree before it is durable. A tree
object in the buffer is visible only to the run that wrote it; after its
flush, every run of the same account can read it, and no other account.

Pack format, version 1: the step bodies and object bytes, back to back, then
a JSON manifest, then the manifest length as an 8-byte big-endian integer.
The manifest names every entry's offset and length and all step fields, so
:meth:`StepLogStore.recover` can rebuild the rows from the pack alone after a
crash between the PUT and the commit.

The fence: the flush locks each run's ``jobs`` row and keeps only the steps
whose ``execution_generation`` is still current. A stale step fails with
:class:`StepFenceError`; the agent sees it on its next commit or on
``durable``.
"""

from __future__ import annotations

import json
import logging
import struct
import threading
import time
from dataclasses import dataclass
from datetime import UTC, datetime
from typing import Any
from uuid import uuid4

from sqlalchemy import func, select
from sqlalchemy.dialects.postgresql import insert as postgres_insert
from sqlalchemy.dialects.sqlite import insert as sqlite_insert
from sqlalchemy.engine import Engine
from sqlalchemy.orm import Session

from ..config import settings
from .base import ObjectStore
from .models import AgentStepModel, JobModel, StoragePackModel, TreeObjectModel

logger = logging.getLogger(__name__)

STEP_KINDS = frozenset({"command", "model"})
PACK_VERSION = 1
PACK_PREFIX = "packs"
_FOOTER = struct.Struct(">Q")
RUN_ID_MAX = 36
AGENT_ID_MAX = 64
SEQ_MAX = 2**31 - 1
_STEP_FIELDS = (
    "step_id",
    "run_id",
    "agent_id",
    "seq",
    "kind",
    "execution_generation",
    "grant_list_id",
    "tree_id",
)


class StepFenceError(RuntimeError):
    """A step failed at flush because its ``execution_generation`` is stale."""

    def __init__(self, step_id: str) -> None:
        """Name the failed step in the error.

        Args:
            step_id: The id of the step that failed.
        """
        super().__init__(f"step {step_id} failed: its execution_generation is stale")
        self.step_id = step_id


def step_id_for(run_id: str, agent_id: str, seq: int) -> str:
    """Return the id of a run's agent step; a retry of the same step gets the same id.

    Args:
        run_id: The job the step belongs to; it holds no ``:``.
        agent_id: The agent that commits the step.
        seq: The step's position in the agent's log.

    Returns:
        ``<run_id>:<agent_id>:<seq>``.
    """
    return f"{run_id}:{agent_id}:{seq}"


@dataclass(frozen=True)
class _Step:
    """One buffered step, its body still in memory."""

    step_id: str
    run_id: str
    agent_id: str
    seq: int
    kind: str
    execution_generation: int
    grant_list_id: str
    tree_id: str | None


@dataclass(frozen=True)
class _Object:
    """One buffered tree object."""

    run_id: str
    object_id: str


def _encode_body(body: dict[str, Any]) -> bytes:
    """Return the canonical JSON bytes of a step body.

    Args:
        body: The step body.

    Returns:
        UTF-8 JSON with sorted keys and no spaces.
    """
    return json.dumps(body, sort_keys=True, separators=(",", ":"), ensure_ascii=False).encode("utf-8")


def _build_pack(worker_id: str, batch: list[tuple[_Step | _Object, bytes]]) -> tuple[bytes, dict[str, Any]]:
    """Lay out a batch as one pack.

    Args:
        worker_id: The worker that writes the pack.
        batch: The buffered entries with their bytes, in commit order.

    Returns:
        The pack bytes and its manifest.
    """
    parts: list[bytes] = []
    steps: list[dict[str, Any]] = []
    objects: list[dict[str, Any]] = []
    offset = 0
    for item, data in batch:
        location = {"offset": offset, "length": len(data)}
        if isinstance(item, _Step):
            steps.append({**{name: getattr(item, name) for name in _STEP_FIELDS}, **location})
        else:
            objects.append({"run_id": item.run_id, "object_id": item.object_id, **location})
        parts.append(data)
        offset += len(data)
    manifest = {"version": PACK_VERSION, "worker_id": worker_id, "steps": steps, "objects": objects}
    footer = json.dumps(manifest, separators=(",", ":")).encode("utf-8")
    return b"".join(parts) + footer + _FOOTER.pack(len(footer)), manifest


def _read_manifest(pack: bytes) -> dict[str, Any]:
    """Return the manifest at the end of a pack.

    Args:
        pack: The whole pack bytes.

    Returns:
        The manifest.
    """
    (length,) = _FOOTER.unpack(pack[-_FOOTER.size :])
    return json.loads(pack[-_FOOTER.size - length : -_FOOTER.size])


class StepLogStore:
    """One worker's pipelined step log over an object store and Postgres."""

    def __init__(
        self,
        engine: Engine,
        objects: ObjectStore,
        worker_id: str,
        *,
        flush_window_seconds: float | None = None,
        flush_max_bytes: int | None = None,
    ) -> None:
        """Bind the store and start its flusher thread.

        Args:
            engine: Engine whose schema carries ``jobs``, ``agent_steps``,
                ``tree_objects`` and ``storage_packs``.
            objects: Where the packs go.
            worker_id: This worker's stable id; its packs go under
                ``packs/<worker_id>/`` so a restarted worker recovers its own.
            flush_window_seconds: The buffer window; ``None`` reads the setting.
            flush_max_bytes: The buffer size cap; ``None`` reads the setting.

        Raises:
            ValueError: When ``worker_id`` is empty, too long or has a ``/``.
        """
        if not worker_id or len(worker_id) > 64 or "/" in worker_id:
            raise ValueError(f"bad worker id: {worker_id!r}")
        self._engine = engine
        self._objects = objects
        self._worker_id = worker_id
        self._window = flush_window_seconds or settings.step_log_flush_window_seconds
        self._max_bytes = flush_max_bytes or settings.step_log_flush_max_bytes
        self._cond = threading.Condition()
        self._flush_lock = threading.Lock()
        self._pending: list[tuple[_Step | _Object, bytes]] = []
        self._pending_bytes = 0
        self._first_at: float | None = None
        # The memtable: buffered and in-flight entries, until their flush commits.
        self._steps: dict[str, tuple[_Step, bytes]] = {}
        # Keyed by run, so an unflushed object never reaches another account and each owner counts its own bytes.
        self._tree_objects: dict[tuple[str, str], bytes] = {}
        self._waiters: dict[str, threading.Event] = {}
        self._failures: dict[str, StepFenceError] = {}
        self._agent_failures: dict[tuple[str, str], StepFenceError] = {}
        self._closed = False
        self._thread = threading.Thread(target=self._run, name=f"step-log-{worker_id}", daemon=True)
        self._thread.start()

    def commit(
        self,
        *,
        run_id: str,
        agent_id: str,
        seq: int,
        kind: str,
        execution_generation: int,
        grant_list_id: str,
        body: dict[str, Any],
        tree_id: str | None = None,
    ) -> str:
        """Buffer one step and return its id at once, before the flush.

        Args:
            run_id: The job id; the shard key.
            agent_id: The agent that commits the step.
            seq: The step's position in the agent's log.
            kind: ``command`` or ``model``.
            execution_generation: The run generation the agent works under.
            grant_list_id: The grant list the step ran with.
            body: JSON body: the command and its output, or the model response.
            tree_id: The workspace tree after a command.

        Returns:
            The step id, the idempotency key: the first commit of an id wins.
            A retry with the same run, agent and ``seq`` returns the same id
            and changes nothing.

        Raises:
            StepFenceError: When an earlier step of this agent in this run
                failed the fence.
            ValueError: When ``kind`` is unknown, a command has no tree id, an
                id is empty or longer than its column, ``run_id`` holds a
                ``:``, or ``seq`` is outside the ``seq`` column.
        """
        # A row the database refuses would fail every later flush of the batch, so refuse it here.
        if not 0 < len(run_id) <= RUN_ID_MAX or not 0 < len(agent_id) <= AGENT_ID_MAX or len(grant_list_id) > 64:
            raise ValueError("run_id, agent_id or grant_list_id is too long or empty")
        # With no ``:`` in the run id and only digits in seq, the first and last ``:`` split every step id one way.
        if ":" in run_id:
            raise ValueError(f"run_id must not hold a ':': {run_id!r}")
        if not 0 <= seq <= SEQ_MAX:
            raise ValueError(f"seq is outside 0..{SEQ_MAX}: {seq}")
        if kind not in STEP_KINDS:
            raise ValueError(f"unknown step kind: {kind!r}")
        if kind == "command" and tree_id is None:
            raise ValueError("a command step needs its new tree id")
        step_id = step_id_for(run_id, agent_id, seq)
        data = _encode_body(body)
        with self._cond:
            failure = self._agent_failures.pop((run_id, agent_id), None)
            if failure is not None:
                raise failure
            if self._closed:
                raise RuntimeError("the step log is closed")
            if step_id in self._steps:
                return step_id
            step = _Step(step_id, run_id, agent_id, seq, kind, execution_generation, grant_list_id, tree_id)
            self._steps[step_id] = (step, data)
            self._waiters[step_id] = threading.Event()
            self._buffer(step, data)
        return step_id

    def put_object(self, run_id: str, object_id: str, data: bytes) -> None:
        """Buffer one content-addressed tree object for the next flush.

        Until the flush, only ``run_id`` reads the object; after it, every
        run of the same account can.

        Args:
            run_id: The job whose owner owns the object.
            object_id: The object's content hash.
            data: The stored object bytes.
        """
        with self._cond:
            if self._closed:
                raise RuntimeError("the step log is closed")
            if (run_id, object_id) in self._tree_objects:
                return
            self._tree_objects[(run_id, object_id)] = data
            self._buffer(_Object(run_id, object_id), data)

    def _buffer(self, item: _Step | _Object, data: bytes) -> None:
        """Add an entry to the pending batch and wake the flusher; the caller holds the lock.

        Args:
            item: The step or object.
            data: Its bytes.
        """
        self._pending.append((item, data))
        self._pending_bytes += len(data)
        if self._first_at is None:
            self._first_at = time.monotonic()
        self._cond.notify_all()

    def durable(self, step_id: str, timeout: float | None = None) -> None:
        """Wait until the step's pack is in the object store and its row is committed.

        Args:
            step_id: The id that :meth:`commit` returned.
            timeout: The most seconds to wait; ``None`` waits without limit.

        Raises:
            StepFenceError: When the step failed the fence.
            TimeoutError: When the flush does not commit in time.
            KeyError: When the store knows no such step.
        """
        with self._cond:
            failure = self._failures.get(step_id)
            event = self._waiters.get(step_id)
        if failure is None and event is not None:
            if not event.wait(timeout):
                raise TimeoutError(f"step {step_id} is not durable yet")
            with self._cond:
                failure = self._failures.get(step_id)
        elif failure is None:
            with Session(self._engine) as session:
                if session.get(AgentStepModel, step_id) is None:
                    raise KeyError(step_id)
        if failure is not None:
            raise failure

    def get_step(self, step_id: str) -> dict[str, Any] | None:
        """Return one step with its body, from the buffer when it is not flushed yet.

        Args:
            step_id: The step id.

        Returns:
            The step fields and ``body``, or ``None`` when no live step has the id.
        """
        with self._cond:
            buffered = self._steps.get(step_id)
        if buffered is not None:
            step, data = buffered
            return {**{name: getattr(step, name) for name in _STEP_FIELDS}, "body": json.loads(data)}
        with Session(self._engine) as session:
            row = session.get(AgentStepModel, step_id)
            if row is None:
                return None
            record = {name: getattr(row, name) for name in _STEP_FIELDS}
            location = (row.pack_key, row.pack_offset, row.pack_length)
        return {**record, "body": json.loads(self._objects.get(*location))}

    def get_object(self, run_id: str, object_id: str) -> bytes | None:
        """Return one tree object's stored bytes for a run, from the buffer when it is not flushed yet.

        Before its flush, an object is visible only to the run that wrote it.
        After the flush, each run of the same account can read it.

        Args:
            run_id: The job that reads the object; its owner must own the object.
            object_id: The object's content hash.

        Returns:
            The stored bytes, or ``None`` when the run is unknown or its owner
            does not have the object.
        """
        with self._cond:
            buffered = self._tree_objects.get((run_id, object_id))
        if buffered is not None:
            return buffered
        # The same ``or ""`` as the flush, so a job without a username reads what the flush stored for it.
        owner = select(func.coalesce(JobModel.username, "")).where(JobModel.optimization_id == run_id).scalar_subquery()
        with Session(self._engine) as session:
            row = session.scalars(
                select(TreeObjectModel).where(TreeObjectModel.object_id == object_id, TreeObjectModel.username == owner)
            ).first()
            if row is None:
                return None
            location = (row.pack_key, row.pack_offset, row.pack_length)
        return self._objects.get(*location)

    def flush(self) -> None:
        """Write the pending batch now: one pack PUT, then one Postgres transaction.

        On a failure the batch goes back to the front of the buffer, so the
        next flush tries it again.
        """
        with self._flush_lock:
            with self._cond:
                batch, first_at = self._pending, self._first_at
                self._pending, self._pending_bytes, self._first_at = [], 0, None
            if not batch:
                return
            try:
                pack, manifest = _build_pack(self._worker_id, batch)
                key = f"{PACK_PREFIX}/{self._worker_id}/{time.time_ns():020d}-{uuid4().hex}.pack"
                self._objects.put(key, pack)
                stale = self._apply(key, len(pack), manifest)
            except Exception:
                with self._cond:
                    self._pending = batch + self._pending
                    self._pending_bytes += sum(len(data) for _, data in batch)
                    self._first_at = first_at
                raise
            with self._cond:
                for entry in stale:
                    error = StepFenceError(entry["step_id"])
                    self._failures[entry["step_id"]] = error
                    self._agent_failures[(entry["run_id"], entry["agent_id"])] = error
                for item, _ in batch:
                    if isinstance(item, _Object):
                        self._tree_objects.pop((item.run_id, item.object_id), None)
                        continue
                    self._steps.pop(item.step_id, None)
                    waiter = self._waiters.pop(item.step_id, None)
                    if waiter is not None:
                        waiter.set()

    def recover(self) -> int:
        """Apply this worker's packs that a crash left without their Postgres transaction.

        A recovered step still passes the fence: it lands only when its
        generation is current.

        Returns:
            The number of packs applied.
        """
        with self._flush_lock:
            with Session(self._engine) as session:
                recorded = set(
                    session.scalars(
                        select(StoragePackModel.pack_key).where(StoragePackModel.worker_id == self._worker_id)
                    )
                )
            applied = 0
            for key in self._objects.list(f"{PACK_PREFIX}/{self._worker_id}/"):
                if key in recorded:
                    continue
                pack = self._objects.get(key)
                self._apply(key, len(pack), _read_manifest(pack))
                applied += 1
            return applied

    def close(self) -> None:
        """Stop the flusher thread, then flush what is left."""
        with self._cond:
            self._closed = True
            self._cond.notify_all()
        self._thread.join()
        self.flush()

    def _apply(self, pack_key: str, byte_size: int, manifest: dict[str, Any]) -> list[dict[str, Any]]:
        """Record one pack's steps and objects in one transaction, behind the fence.

        The ``storage_packs`` row makes the apply happen once: a second apply
        of the same pack, by recovery or a retry, finds the row and stops.

        Args:
            pack_key: The pack's object key.
            byte_size: The pack's size.
            manifest: The pack's manifest.

        Returns:
            The manifest entries of the steps that failed the fence.
        """
        insert = postgres_insert if self._engine.dialect.name == "postgresql" else sqlite_insert
        now = datetime.now(UTC)
        steps, objects = manifest["steps"], manifest["objects"]
        run_ids = sorted({entry["run_id"] for entry in steps + objects})
        with Session(self._engine) as session:
            # A fixed lock order keeps two workers' flushes from deadlocking.
            jobs = {
                row.optimization_id: row
                for row in session.execute(
                    select(JobModel.optimization_id, JobModel.execution_generation, JobModel.username)
                    .where(JobModel.optimization_id.in_(run_ids))
                    .order_by(JobModel.optimization_id)
                    .with_for_update()
                )
            }
            claimed = session.execute(
                insert(StoragePackModel)
                .values(pack_key=pack_key, worker_id=manifest["worker_id"], byte_size=byte_size, created_at=now)
                .on_conflict_do_nothing()
            ).rowcount
            if not claimed:
                session.rollback()
                return []
            fresh, stale = [], []
            for entry in steps:
                job = jobs.get(entry["run_id"])
                current = job is not None and job.execution_generation == entry["execution_generation"]
                (fresh if current else stale).append(entry)
            if fresh:
                session.execute(
                    insert(AgentStepModel)
                    .values(
                        [
                            {
                                **{name: entry[name] for name in _STEP_FIELDS},
                                "pack_key": pack_key,
                                "pack_offset": entry["offset"],
                                "pack_length": entry["length"],
                                "created_at": now,
                            }
                            for entry in fresh
                        ]
                    )
                    .on_conflict_do_nothing()
                )
            owned = {
                (jobs[entry["run_id"]].username or "", entry["object_id"]): entry
                for entry in objects
                if entry["run_id"] in jobs
            }
            if owned:
                session.execute(
                    insert(TreeObjectModel)
                    .values(
                        [
                            {
                                "username": username,
                                "object_id": object_id,
                                "byte_size": entry["length"],
                                "pack_key": pack_key,
                                "pack_offset": entry["offset"],
                                "pack_length": entry["length"],
                                "created_at": now,
                            }
                            for (username, object_id), entry in owned.items()
                        ]
                    )
                    .on_conflict_do_nothing()
                )
            session.commit()
        return stale

    def _due(self) -> bool:
        """Return whether the pending batch must flush now; the caller holds the lock."""
        if not self._pending or self._first_at is None:
            return False
        return self._pending_bytes >= self._max_bytes or time.monotonic() - self._first_at >= self._window

    def _wait_seconds(self) -> float | None:
        """Return how long the flusher may sleep; the caller holds the lock."""
        if self._first_at is None:
            return None
        return max(0.0, self._first_at + self._window - time.monotonic())

    def _run(self) -> None:
        """Flush each batch when its window ends or it reaches the size cap, until close."""
        while True:
            with self._cond:
                while not self._closed and not self._due():
                    self._cond.wait(self._wait_seconds())
                if self._closed:
                    return
            try:
                self.flush()
            except Exception:
                logger.exception("step log flush failed; it tries again after the window")
                with self._cond:
                    if not self._closed:
                        self._cond.wait(self._window)
