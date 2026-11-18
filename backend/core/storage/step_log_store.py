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
:class:`StepFenceError`, also when a row with its step id exists.
``durable`` raises it, and the agent's next commit at that generation raises
it one time; a commit after that is buffered and gets its own verdict at its
flush. The step id holds no generation, so the buffer and each verdict are
kept by step id and generation. The store keeps only the newest
``FENCE_VERDICT_MAX`` verdicts of a step and of an agent in memory. A kept
verdict is the step id, not an error: each raise makes a new error.
"""

from __future__ import annotations

import json
import logging
import re
import struct
import threading
import time
from dataclasses import dataclass
from datetime import UTC, datetime
from typing import Any
from uuid import uuid4

from sqlalchemy import func, select, update
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
# The byte_size of the row that the compactor's sweep inserts to claim an old flush pack with no row.
# An applied pack is never empty: it always holds its manifest.
CLAIM_BYTE_SIZE = 0
# A flush pack with no row is deleted only after this age. A flush keeps retrying the apply of its
# written pack while its pod lives, a job lease is 60 seconds, and a pod that restarts under its name
# applies its own packs at start. Seven days is far past all of them, so no apply is still in flight.
# A pod that comes back under a new name never applies its packs, so without this sweep their bytes,
# also those of a deleted account, would stay forever (SKY-733). An apply refuses a pack of this age
# itself: the sweep's claim row goes after the grace time, so it cannot keep a later apply out alone.
UNRECORDED_FLUSH_PACK_AGE_SECONDS = 7 * 24 * 3600.0
_FOOTER = struct.Struct(">Q")
RUN_ID_MAX = 36
AGENT_ID_MAX = 64
WORKER_ID_MAX = 64
GRANT_LIST_ID_MAX = 64
SEQ_MAX = 2**31 - 1
# Each stale step of a flush adds one verdict for its step and one for its agent, also when the agent stops after it.
# A verdict is only a key and a step id: the two dicts keep about 0.4 KB for a stale step with a 60-character id.
FENCE_VERDICT_MAX = 10_000
OBJECT_ID = re.compile(r"[0-9a-f]{64}")
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


class PackClaimedError(RuntimeError):
    """The compactor's sweep claimed a flush pack, or may delete it, before any store recorded it.

    None of the pack's steps landed.
    """

    def __init__(self, pack_key: str) -> None:
        """Name the claimed pack in the error.

        Args:
            pack_key: The key of the claimed pack.
        """
        super().__init__(
            f"pack {pack_key} was claimed by the sweep, or is past its age, before it was recorded;"
            " none of its steps landed"
        )
        self.pack_key = pack_key


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


def _is_text_id(value: str) -> bool:
    """Return whether an id is printable ASCII.

    PostgreSQL text refuses a NUL, and the driver's UTF-8 encode fails on a
    lone surrogate.

    Args:
        value: The id.

    Returns:
        Whether the database takes the id as it is.
    """
    return value.isascii() and value.isprintable()


def _check_run_id(run_id: str) -> None:
    """Refuse a run id that the database or the step id would refuse.

    Args:
        run_id: The job id.

    Raises:
        ValueError: When the id is empty, too long, not printable ASCII, or holds a ``:``.
    """
    if not 0 < len(run_id) <= RUN_ID_MAX or not _is_text_id(run_id):
        raise ValueError(f"run_id is empty, too long or not printable ASCII: {run_id!r}")
    # With no ``:`` in the run id and only digits in seq, the first and last ``:`` split every step id one way.
    if ":" in run_id:
        raise ValueError(f"run_id must not hold a ':': {run_id!r}")


def _past_sweep_age(pack_key: str) -> bool:
    """Return whether a flush pack's key time is older than ``UNRECORDED_FLUSH_PACK_AGE_SECONDS``.

    Args:
        pack_key: The pack key, ``packs/<worker>/<time>-<id>.pack``.

    Returns:
        ``False`` also for a key with no write time: the sweep never deletes such a pack.
    """
    try:
        written_ns = int(pack_key.rsplit("/", 1)[-1][:20])
    except ValueError:
        return False
    return time.time_ns() - written_ns >= int(UNRECORDED_FLUSH_PACK_AGE_SECONDS * 1e9)


def _keep_verdict(verdicts: dict[Any, str], key: Any, step_id: str) -> None:
    """Keep a fence verdict as the newest one and drop the oldest one above ``FENCE_VERDICT_MAX``.

    Args:
        verdicts: The verdicts by key, oldest first.
        key: The key of the new verdict.
        step_id: The verdict: the id of the fenced step.
    """
    verdicts.pop(key, None)
    verdicts[key] = step_id
    if len(verdicts) > FENCE_VERDICT_MAX:
        del verdicts[next(iter(verdicts))]


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


class _Waiter(threading.Event):
    """The durable barrier of one buffered step, with the fence verdict that its flush sets before it opens."""

    # On the waiter, not only in a dict by key: a retry of the same key can replace the dict entry first.
    # The value is the id of the fenced step; ``None`` means that the step passed the fence.
    failure: str | None = None
    # Set when the last flush of ``close`` failed: no flusher is left to open the waiter.
    closed: bool = False
    # Set when the compactor's sweep claimed the step's pack before the flush recorded it.
    lost: bool = False


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
            flush_window_seconds: The buffer window, above 0 and at most
                ``threading.TIMEOUT_MAX``; ``None`` reads the setting.
            flush_max_bytes: The buffer size cap, at least 1; ``None`` reads the setting.

        Raises:
            ValueError: When ``worker_id`` is empty, too long, not printable
                ASCII, has a ``/``, or is ``.`` or ``..``; or when the window is
                not above 0 or is above ``threading.TIMEOUT_MAX``, or the size
                cap is below 1.
        """
        # ``.`` and ``..`` put packs outside ``packs/<worker_id>/``, where recovery never lists them.
        if (
            not 0 < len(worker_id) <= WORKER_ID_MAX
            or not _is_text_id(worker_id)
            or "/" in worker_id
            or worker_id in {".", ".."}
        ):
            raise ValueError(f"bad worker id: {worker_id!r}")
        self._engine = engine
        self._objects = objects
        self._worker_id = worker_id
        window = settings.step_log_flush_window_seconds if flush_window_seconds is None else flush_window_seconds
        max_bytes = settings.step_log_flush_max_bytes if flush_max_bytes is None else flush_max_bytes
        # A window of 0 or less makes the flusher retry a failed flush with no wait. A wait above
        # ``TIMEOUT_MAX`` raises in the flusher thread and ends it, so no later commit lands.
        if not 0 < window <= threading.TIMEOUT_MAX or max_bytes < 1:
            raise ValueError(f"bad flush window or size cap: {window!r}, {max_bytes!r}")
        self._window = window
        self._max_bytes = max_bytes
        self._cond = threading.Condition()
        self._flush_lock = threading.Lock()
        self._pending: list[tuple[_Step | _Object, bytes]] = []
        self._pending_bytes = 0
        self._first_at: float | None = None
        # A pack in the object store whose transaction failed: its key, size, manifest and batch.
        # Its entries stay in the memtable and its waiters stay open until a flush records it.
        self._unapplied: tuple[str, int, dict[str, Any], list[tuple[_Step | _Object, bytes]]] | None = None
        # The memtable: buffered and in-flight entries by step id, then by generation, until their flush commits.
        self._steps: dict[str, dict[int, tuple[_Step, bytes]]] = {}
        # Keyed by run, so an unflushed object never reaches another account and each owner counts its own bytes.
        self._tree_objects: dict[tuple[str, str], bytes] = {}
        # Keyed by step id and generation: two generations of one step id get their own verdicts.
        self._waiters: dict[tuple[str, int], _Waiter] = {}
        # For a ``durable`` call that comes after the flush removed the step's waiter. Oldest first, with a cap.
        # A verdict is the step id, never an error object: each raise adds the frames of its callers to the error.
        self._failures: dict[tuple[str, int], str] = {}
        # Keyed by generation too: a verdict of an old generation does not fail a commit of the run's new generation.
        self._agent_failures: dict[tuple[str, str, int], str] = {}
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
            The step id, the idempotency key: among current-generation
            commits, the first commit of an id wins. A retry with the same
            run, agent, ``seq`` and generation, while the first commit waits
            for its flush, returns the same id and changes nothing. A retry
            after that flush is buffered again: a stored row stays as it is,
            and a step that failed the fence gets a new verdict at the new
            flush. A commit of the same id at another generation is buffered
            on its own and gets its own verdict.

        Raises:
            StepFenceError: One time, on the first commit of this agent at
                this run and generation after one of its steps failed the
                fence. A commit after that is buffered and gets its own
                verdict at its flush.
            ValueError: When ``kind`` is unknown, a command has no tree id, an
                id is empty, longer than its column or not printable ASCII,
                ``run_id`` holds a ``:``, ``tree_id`` is not 64 lowercase hex
                characters, ``seq`` is not an ``int`` in the ``seq`` column, or
                ``execution_generation`` is not an ``int``.
        """
        # A row the database refuses would fail every later flush of the batch, so refuse it here.
        _check_run_id(run_id)
        if not 0 < len(agent_id) <= AGENT_ID_MAX or not 0 < len(grant_list_id) <= GRANT_LIST_ID_MAX:
            raise ValueError("agent_id or grant_list_id is too long or empty")
        if not _is_text_id(agent_id) or not _is_text_id(grant_list_id):
            raise ValueError("agent_id or grant_list_id is not printable ASCII")
        if type(seq) is not int or not 0 <= seq <= SEQ_MAX:
            raise ValueError(f"seq is not an int in 0..{SEQ_MAX}: {seq!r}")
        # ``True`` and ``1.0`` equal generation 1 at the fence, and the integer column refuses them.
        if type(execution_generation) is not int:
            raise ValueError(f"execution_generation is not an int: {execution_generation!r}")
        if tree_id is not None and not OBJECT_ID.fullmatch(tree_id):
            raise ValueError(f"tree_id is not 64 lowercase hex characters: {tree_id!r}")
        if kind not in STEP_KINDS:
            raise ValueError(f"unknown step kind: {kind!r}")
        if kind == "command" and tree_id is None:
            raise ValueError("a command step needs its new tree id")
        step_id = step_id_for(run_id, agent_id, seq)
        key = (step_id, execution_generation)
        data = _encode_body(body)
        with self._cond:
            failure = self._agent_failures.pop((run_id, agent_id, execution_generation), None)
            if failure is not None:
                raise StepFenceError(failure)
            if self._closed:
                raise RuntimeError("the step log is closed")
            versions = self._steps.setdefault(step_id, {})
            if execution_generation in versions:
                return step_id
            step = _Step(step_id, run_id, agent_id, seq, kind, execution_generation, grant_list_id, tree_id)
            # A new commit of a fenced step at the same generation gets a new verdict at its own flush.
            self._failures.pop(key, None)
            versions[execution_generation] = (step, data)
            self._waiters[key] = _Waiter()
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

        Raises:
            ValueError: When ``run_id`` is not a valid run id or
                ``object_id`` is not 64 lowercase hex characters.
            TypeError: When ``data`` is not exactly ``bytes``.
        """
        # As in ``commit``: an id the database refuses would fail every later flush of the batch.
        _check_run_id(run_id)
        if not OBJECT_ID.fullmatch(object_id):
            raise ValueError(f"object_id is not 64 lowercase hex characters: {object_id!r}")
        # Data the pack cannot hold would also fail every later flush. A ``bytearray`` can change
        # after the call, and then the buffered bytes no longer match the object id.
        if type(data) is not bytes:
            raise TypeError(f"data is not bytes: {type(data).__name__}")
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

    def durable(self, step_id: str, execution_generation: int, timeout: float | None = None) -> None:
        """Wait until the step's pack is in the object store and its row is committed.

        When the store holds no waiter and no verdict for the step at this
        generation, the database answers: the step is durable only when its
        row exists and the generation is still the job's current one. That is
        the case after a restart, and for a fence verdict that left the
        store's bounded memory; a fenced step with no row then gives
        ``KeyError``.

        Args:
            step_id: The id that :meth:`commit` returned.
            execution_generation: The generation the step was committed under.
            timeout: The most seconds to wait; ``None`` waits without limit.

        Raises:
            StepFenceError: When the step failed the fence, or its generation
                is no longer current.
            TimeoutError: When the flush does not commit in time.
            KeyError: When the store knows no such step and it has no row.
            RuntimeError: When the last flush of :meth:`close` failed before
                the step was durable, or the compactor's sweep deleted the
                step's pack before a flush recorded it.
        """
        key = (step_id, execution_generation)
        with self._cond:
            failure = self._failures.get(key)
            waiter = self._waiters.get(key)
        if failure is None and waiter is not None:
            if not waiter.wait(timeout):
                raise TimeoutError(f"step {step_id} is not durable yet")
            if waiter.closed:
                raise RuntimeError(f"the step log closed before step {step_id} was durable")
            if waiter.lost:
                raise RuntimeError(f"the pack of step {step_id} was deleted before it was recorded")
            failure = waiter.failure
        elif failure is None:
            with Session(self._engine) as session:
                row = session.get(AgentStepModel, step_id)
                if row is None:
                    raise KeyError(step_id)
                current = session.scalar(
                    select(JobModel.execution_generation).where(JobModel.optimization_id == row.run_id)
                )
            if current != execution_generation:
                failure = step_id
        if failure is not None:
            raise StepFenceError(failure)

    def get_step(self, step_id: str) -> dict[str, Any] | None:
        """Return one step with its body: the stored row first, else the buffer when it is not flushed yet.

        A retry of a stored step sits in the buffer until its flush; the
        stored row wins, so a reader always sees the first commit. With no
        row, the buffered entry of the highest generation wins.

        Args:
            step_id: The step id.

        Returns:
            The step fields and ``body``, or ``None`` when no live step has the id.
        """
        # Read the buffer before the row: the flush drops a step from the buffer only after its row commits.
        with self._cond:
            versions = self._steps.get(step_id)
            buffered = versions[max(versions)] if versions else None
        with Session(self._engine) as session:
            row = session.get(AgentStepModel, step_id)
            if row is not None:
                record = {name: getattr(row, name) for name in _STEP_FIELDS}
                location = (row.pack_key, row.pack_offset, row.pack_length)
        if row is not None:
            return {**record, "body": json.loads(self._objects.get(*location))}
        if buffered is None:
            return None
        step, data = buffered
        return {**{name: getattr(step, name) for name in _STEP_FIELDS}, "body": json.loads(data)}

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

        When the PUT fails, the batch goes back to the front of the buffer.
        When the transaction fails after the PUT, the store keeps the written
        pack, and the next flush records that pack first, with no new PUT.
        Until then, the next flush writes no new pack.
        """
        with self._flush_lock:
            if self._unapplied is not None:
                key, size, manifest, batch = self._unapplied
                lost = False
                try:
                    _, stale = self._apply(key, size, manifest)
                except PackClaimedError:
                    # A retry that outlived UNRECORDED_FLUSH_PACK_AGE_SECONDS: the bytes are gone, so the
                    # batch's waiters fail rather than wait for a pack that no retry can record.
                    logger.exception("Flush pack %s was claimed by the sweep before it was recorded", key)
                    lost, stale = True, []
                with self._cond:
                    self._unapplied = None
                self._settle(batch, stale, lost=lost)
            with self._cond:
                batch, first_at = self._pending, self._first_at
                self._pending, self._pending_bytes, self._first_at = [], 0, None
            if not batch:
                return
            try:
                pack, manifest = _build_pack(self._worker_id, batch)
                key = f"{PACK_PREFIX}/{self._worker_id}/{time.time_ns():020d}-{uuid4().hex}.pack"
                self._objects.put(key, pack)
            except Exception:
                with self._cond:
                    self._pending = batch + self._pending
                    self._pending_bytes += sum(len(data) for _, data in batch)
                    self._first_at = first_at
                    # An outside flush took the batch while the flusher slept on an empty buffer with no deadline.
                    self._cond.notify_all()
                raise
            try:
                _, stale = self._apply(key, len(pack), manifest)
            except Exception:
                # A new pack of the same batch on each retry makes the PUT bytes grow with each failed window.
                with self._cond:
                    self._unapplied = (key, len(pack), manifest, batch)
                    self._cond.notify_all()
                raise
            self._settle(batch, stale)

    def _settle(
        self, batch: list[tuple[_Step | _Object, bytes]], stale: list[dict[str, Any]], lost: bool = False
    ) -> None:
        """Drop a recorded batch from the buffer and open its waiters with their fence verdicts.

        When the pack is lost, each agent of the batch gets a verdict, and its pending steps leave the
        buffer with a fence verdict: a later step can name a tree whose objects were in the lost pack.

        Args:
            batch: The entries of the recorded pack.
            stale: The manifest entries of the batch's steps that did not land.
            lost: Whether the sweep claimed the pack first, so every waiter opens with an error.
        """
        with self._cond:
            # The batch's waiters read their verdict here: the cap of ``_failures`` can remove one of this flush.
            verdicts: dict[tuple[str, int], str] = {}
            for entry in stale:
                step_key = (entry["step_id"], entry["execution_generation"])
                verdicts[step_key] = entry["step_id"]
                _keep_verdict(self._failures, step_key, entry["step_id"])
                agent_key = (entry["run_id"], entry["agent_id"], entry["execution_generation"])
                _keep_verdict(self._agent_failures, agent_key, entry["step_id"])
            dropped: list[tuple[_Step | _Object, bytes]] = []
            if lost:
                lost_agents: dict[tuple[str, str, int], str] = {}
                for item, _ in batch:
                    if isinstance(item, _Step):
                        lost_agents.setdefault((item.run_id, item.agent_id, item.execution_generation), item.step_id)
                for agent_key, step_id in lost_agents.items():
                    _keep_verdict(self._agent_failures, agent_key, step_id)
                # Under the same lock as the verdicts, so no later step of a lost agent stays in the buffer.
                kept: list[tuple[_Step | _Object, bytes]] = []
                for entry in self._pending:
                    item = entry[0]
                    if (
                        isinstance(item, _Step)
                        and (item.run_id, item.agent_id, item.execution_generation) in lost_agents
                    ):
                        dropped.append(entry)
                        step_key = (item.step_id, item.execution_generation)
                        verdicts[step_key] = item.step_id
                        _keep_verdict(self._failures, step_key, item.step_id)
                    else:
                        kept.append(entry)
                self._pending = kept
                self._pending_bytes = sum(len(data) for _, data in kept)
                if not kept:
                    self._first_at = None
            for item, _ in batch + dropped:
                if isinstance(item, _Object):
                    self._tree_objects.pop((item.run_id, item.object_id), None)
                    continue
                versions = self._steps.get(item.step_id)
                if versions is not None:
                    versions.pop(item.execution_generation, None)
                    if not versions:
                        del self._steps[item.step_id]
                step_key = (item.step_id, item.execution_generation)
                waiter = self._waiters.pop(step_key, None)
                if waiter is not None:
                    waiter.failure = verdicts.get(step_key)
                    waiter.lost = lost and step_key not in verdicts
                    waiter.set()

    def recover(self) -> int:
        """Apply this worker's packs that a crash left without their Postgres transaction.

        A recovered step still passes the fence: it lands only when its
        generation is current.

        Returns:
            The number of packs that this call applied; a pack that another
            store applied first does not count.
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
                try:
                    pack = self._objects.get(key)
                except KeyError:
                    # The compactor's sweep claimed and deleted the pack after the list. A late apply of
                    # the pack is refused by its claim row, and after the row goes by the pack's age.
                    continue
                try:
                    claimed, _ = self._apply(key, len(pack), _read_manifest(pack))
                except PackClaimedError:
                    # The sweep claimed the pack after the list; the claim row keeps its steps out.
                    continue
                applied += claimed
            return applied

    def close(self) -> None:
        """Stop the flusher thread, then flush what is left.

        When that last flush fails, each open waiter opens with an error,
        because no flusher is left to retry.
        """
        with self._cond:
            self._closed = True
            self._cond.notify_all()
        self._thread.join()
        try:
            self.flush()
        except Exception:
            with self._cond:
                for waiter in self._waiters.values():
                    waiter.closed = True
                    waiter.set()
            raise

    def _apply(self, pack_key: str, byte_size: int, manifest: dict[str, Any]) -> tuple[bool, list[dict[str, Any]]]:
        """Record one pack's steps and objects in one transaction, behind the fence.

        The ``storage_packs`` row makes the apply happen once: a second apply
        of the same pack, by recovery or a retry, finds the row and stops.
        A row that the compactor's sweep inserted to claim the pack is not an
        apply: none of the pack's entries got a row.
        The fence comes first: a step whose generation is not the job's
        current one fails, also when a row with its id exists. A current step
        whose id has a row is done and not inserted again, so the first
        commit of a step id wins.

        When an entry gets no row (a stale step, a step or object that
        already has a row, or an object of a job that is gone), the pack
        gets ``needs_rewrite``, so the compactor drops the entry's bytes.

        Args:
            pack_key: The pack's object key.
            byte_size: The pack's size.
            manifest: The pack's manifest.

        Returns:
            Whether this call claimed and applied the pack, and the manifest
            entries of the steps that did not land through this pack. When
            this call applied the pack, those are the steps that failed the
            fence. When another store applied it first, those are the steps
            with a stale generation and the current steps with no row: the
            other store fenced them.

        Raises:
            PackClaimedError: When the row is the sweep's claim, so no store
                applied the pack, or when no store applied the flush pack
                before it reached ``UNRECORDED_FLUSH_PACK_AGE_SECONDS``.
        """
        insert = postgres_insert if self._engine.dialect.name == "postgresql" else sqlite_insert
        now = datetime.now(UTC)
        steps, objects = manifest["steps"], manifest["objects"]
        run_ids = sorted({entry["run_id"] for entry in steps + objects})
        with Session(self._engine) as session:
            # One order of the ``jobs`` row locks keeps two flushes of the same runs from deadlocking.
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
            ids = [entry["step_id"] for entry in steps]
            stored = set(session.scalars(select(AgentStepModel.step_id).where(AgentStepModel.step_id.in_(ids))))
            current = {(job.optimization_id, job.execution_generation) for job in jobs.values()}
            fresh, stale = [], []
            for entry in steps:
                if (entry["run_id"], entry["execution_generation"]) not in current:
                    stale.append(entry)
                # A current step with a row is a retry of an applied step: it is done.
                elif entry["step_id"] not in stored:
                    fresh.append(entry)
            # Checked after the insert, so a wait on the job locks cannot make the check stale. The sweep's
            # claim row goes after the grace time; then only this age keeps an apply of a deleted pack out.
            if claimed and pack_key.startswith(f"{PACK_PREFIX}/") and _past_sweep_age(pack_key):
                session.rollback()
                raise PackClaimedError(pack_key)
            if not claimed:
                row_size = session.scalar(
                    select(StoragePackModel.byte_size).where(StoragePackModel.pack_key == pack_key)
                )
                session.rollback()
                if row_size == CLAIM_BYTE_SIZE:
                    raise PackClaimedError(pack_key)
                # Another store applied this pack and inserted every current step that had no row then,
                # so a current step with no row now failed its fence.
                return False, stale + fresh
            landed = 0
            if fresh:
                landed += session.execute(
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
                ).rowcount
            owned = {
                (jobs[entry["run_id"]].username or "", entry["object_id"]): entry
                for entry in objects
                if entry["run_id"] in jobs
            }
            if owned:
                # Flushes of other runs of the account share these keys; sorted rows give each flush one lock order.
                landed += session.execute(
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
                            for (username, object_id), entry in sorted(owned.items())
                        ]
                    )
                    .on_conflict_do_nothing()
                ).rowcount
            if landed < len(steps) + len(objects):
                # No row points at those bytes. A pack over half the merge target is a candidate only with
                # the mark, so without it the bytes of a deleted account could stay forever (SKY-733).
                session.execute(
                    update(StoragePackModel).where(StoragePackModel.pack_key == pack_key).values(needs_rewrite=True)
                )
            session.commit()
        return True, stale

    def _due(self) -> bool:
        """Return whether a flush must run now; the caller holds the lock."""
        # With no new commit, the kept pack's waiters would wait for a flush that never starts.
        if self._unapplied is not None:
            return True
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
                # A fixed deadline: each commit notifies, and a retry on every commit would PUT the batch again each time.
                deadline = time.monotonic() + self._window
                with self._cond:
                    while not self._closed and (left := deadline - time.monotonic()) > 0:
                        self._cond.wait(left)
