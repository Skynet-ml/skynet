"""The tree store compactor: merge small step log packs into larger ones (SKY-716).

Each step log flush writes one small pack. A pass of the compactor takes the
oldest small packs that have a ``storage_packs`` row, copies the entries
that rows still point at into one new pack under ``compacted/``, and in one
transaction points every ``agent_steps`` and ``tree_objects`` row at the new
pack. It never deletes a tree or a step: only old pack objects go.

The order makes a kill at any point safe:

1. The pass reads the candidates and their rows, then GETs the old packs and
   PUTs the new pack, with no transaction open. A kill before the commit
   leaves the rows on the old packs and a new pack with no row, which a
   later pass deletes once its key time is older than the grace time.
2. A short transaction locks the jobs of the moved steps and the old packs,
   each in key order with NOWAIT, and moves each row only while it still
   points at its old pack. When a lock is held by another transaction, or a
   row moved or was deleted, the pass rolls back and deletes its new pack.
   It also gives up when half the grace time passed since the key time, so
   another replica never deletes a new pack that is about to commit.
3. The same transaction adds the new pack's ``storage_packs`` row and sets
   each old pack's ``created_at`` to the commit time, which is its retire
   time.
4. A later pass deletes a pack that no row points at only when its
   ``created_at`` is older than the grace time, so a reader that read a row
   before the move can still read the old pack. It deletes the object
   first and the ``storage_packs`` row last.

Only packs with a ``storage_packs`` row are merged, so a pack that a flush
wrote but did not apply yet stays for :meth:`StepLogStore.recover`. A flush
pack that still has no row after ``UNRECORDED_FLUSH_PACK_AGE_SECONDS`` is
deleted: no worker applies it any more. The sweep first claims the pack with
a ``storage_packs`` row of no rows and the pack's worker id, the same insert
that an apply makes. Only one of the two wins, so recovery never applies a
pack that the sweep deletes. The claim row is a retired pack, and step 4
deletes it after the grace time. The new packs are outside ``packs/``, where
recovery never lists them, and their rows commit in the same transaction as
the moved rows.

The sweep of the packs with no row lists the whole prefix, so it runs at most
once each ``SWEEP_INTERVAL_SECONDS`` on a replica, and on PostgreSQL on one
replica at a time, under an advisory lock that a busy replica skips.

A compacted pack of at most half of ``target_bytes`` is a candidate again, so
a byte can be copied in more than one pass. This is acceptable: a pass copies
at most ``target_bytes`` and runs at most once an interval on each replica.
A pack leaves the candidates once it holds more than half of
``target_bytes``, so any two candidates fit in the bytes of one pass, and an
old large pack cannot stop the merges of the newer ones. The row rule applies
only to compacted packs: a compacted pack with more than half of
``MAX_ROWS_PER_PASS`` rows also leaves the candidates. A flush pack has no
such mark, so a pass skips a flush pack whose rows do not fit and goes on.
Size tiers are a later change if the copy volume shows a need.

A pack can hold entries that no row points at: an account deletion leaves the
bytes of the deleted account in each pack that other accounts also use
(:mod:`core.storage.tree_purge`), and a flush can write a stale step, a
duplicate, or an entry of a job that is gone. The purge and the flush set
``needs_rewrite`` on such a pack. A marked pack is a candidate at any size and
comes first, and a pass may rewrite it alone. The rewrite copies only the
entries that rows still point at, so the deleted bytes go when the old pack is
retired and deleted. A marked pack with ``MAX_ROWS_PER_PASS`` rows or more
moves in parts: each pass moves its first ``MAX_ROWS_PER_PASS - 1`` rows by
offset, alone. The pack keeps its mark until its last row moves.
"""

from __future__ import annotations

import logging
import threading
import time
from collections.abc import Callable
from datetime import UTC, datetime, timedelta
from typing import Any
from uuid import uuid4

from sqlalchemy import ColumnElement, Table, Update, and_, case, delete, exists, or_, select, text, update
from sqlalchemy.dialects.postgresql import insert as postgres_insert
from sqlalchemy.dialects.sqlite import insert as sqlite_insert
from sqlalchemy.engine import Engine
from sqlalchemy.exc import OperationalError
from sqlalchemy.orm import Session

from ..config import settings
from .base import ObjectStore
from .models import AgentStepModel, JobModel, StoragePackModel, TreeObjectModel
from .object_store import S3ObjectStore
from .step_log_store import CLAIM_BYTE_SIZE, PACK_PREFIX

try:
    import boto3
except ImportError:
    boto3 = None

logger = logging.getLogger(__name__)

COMPACTED_PREFIX = "compacted"
COMPACTOR_ID = "compactor"
# The worker id of a merged pack with more than half the row cap. Two such packs never fit in one pass, so
# the candidate query leaves them out; otherwise they fill the oldest-first window and merging stops.
# A step log worker id never holds "/", so recovery never selects these rows.
FULL_COMPACTOR_ID = f"{COMPACTOR_ID}/full"
# The byte size of a pack whose object is missing or shorter than its rows. Its rows stay on it, and it
# leaves the candidates, so it does not hold the oldest-first window on every pass. The mark is not a
# worker id: a short flush pack still has its object under packs/<worker>/, and recovery counts a pack
# as recorded only when its row keeps that worker's id. Only the candidate query reads this column.
BROKEN_BYTE_SIZE = -1
# PostgreSQL's lock_not_available: a NOWAIT lock found the row held by another transaction.
_LOCK_NOT_AVAILABLE = "55P03"
# The S3 error code in a boto3 ClientError response; read from the dict, so boto3 need not be installed.
_INVALID_RANGE = "InvalidRange"
MIN_AGE_SECONDS = 600.0
# Much longer than a read of a row and then of its pack range.
GRACE_SECONDS = 600.0
TARGET_BYTES = 64 * 1024 * 1024
MAX_PACKS_PER_PASS = 1000
# A pass must commit inside half the grace time, so the rows it moves are capped as well as its bytes.
# A pass adds a pack only when its rows fit under the cap. A pack with this many rows is full, as a
# pack at the target size is: it is never merged again. A merged pack over half the cap gets
# FULL_COMPACTOR_ID and is not a candidate.
MAX_ROWS_PER_PASS = 100_000
# Rows in one UPDATE or keys in one IN query; at most 6 binds a row stays far below the SQLite bind limit.
CHUNK_SIZE = 200
INTERVAL_SECONDS = 60.0
# A flush pack with no row is deleted only after this age. A flush keeps retrying the apply of its
# written pack while its pod lives, a job lease is 60 seconds, and a pod that restarts under its name
# applies its own packs at start. Seven days is far past all of them, so no apply is still in flight.
# A pod that comes back under a new name never applies its packs, so without this sweep their bytes,
# also those of a deleted account, would stay forever (SKY-733).
UNRECORDED_FLUSH_PACK_AGE_SECONDS = 7 * 24 * 3600.0
# Each sweep of the packs with no row lists all of packs/ and compacted/; the LIST cost must not grow with
# the pass interval or the replica count.
SWEEP_INTERVAL_SECONDS = 3600.0
# A stable fleet-wide pg_try_advisory_xact_lock key; the 742137000001-8 keys are in use elsewhere.
SWEEP_LOCK_KEY = 742137000009

# A pack is in use while a row points at it. The step log shards (SKY-731) add their reference here.
REFERENCED = or_(
    exists().where(AgentStepModel.pack_key == StoragePackModel.pack_key),
    exists().where(TreeObjectModel.pack_key == StoragePackModel.pack_key),
)
_STEPS = AgentStepModel.__table__
_OBJECTS = TreeObjectModel.__table__


def _chunks(items: list[Any]) -> list[list[Any]]:
    """Split a list into chunks of at most ``CHUNK_SIZE`` items.

    Args:
        items: The list.

    Returns:
        The chunks, in order.
    """
    return [items[start : start + CHUNK_SIZE] for start in range(0, len(items), CHUNK_SIZE)]


def tree_bucket_client() -> tuple[str, Any] | None:
    """Return the tree bucket that ``SKYNET_TREE_BUCKET`` names, with an S3 client.

    Returns:
        The bucket name and the client, or ``None`` when no bucket is set.

    Raises:
        RuntimeError: When a bucket is set and boto3 is not installed.
    """
    bucket = settings.tree_bucket
    if not bucket:
        return None
    if boto3 is None:
        raise RuntimeError("boto3 is not installed")
    return bucket, boto3.client("s3")


def _move_chunk(table: Table, id_names: tuple[str, ...], chunk: list[tuple[Any, int]], new_key: str) -> Update:
    """Build one UPDATE that points each row of a chunk at its offset in the new pack.

    Args:
        table: ``agent_steps`` or ``tree_objects``.
        id_names: The primary key columns of the table.
        chunk: Each row with its offset in the new pack.
        new_key: The new pack key.

    Returns:
        The statement.
    """

    def matches(row: Any) -> ColumnElement[bool]:
        """Match one row by its primary key.

        Args:
            row: The row as read.

        Returns:
            The condition.
        """
        return and_(*(table.c[name] == getattr(row, name) for name in id_names))

    # The old pack key in each condition leaves a row that another pass moved first where it is.
    return (
        update(table)
        .where(or_(*(and_(matches(row), table.c.pack_key == row.pack_key) for row, _ in chunk)))
        .values(pack_key=new_key, pack_offset=case(*((matches(row), offset) for row, offset in chunk)))
    )


class TreeCompactor:
    """Merge small packs into larger ones, and delete the packs that no row points at."""

    def __init__(
        self,
        engine: Engine,
        objects: ObjectStore,
        delete_object: Callable[[str], None],
        *,
        min_age_seconds: float = MIN_AGE_SECONDS,
        grace_seconds: float = GRACE_SECONDS,
        target_bytes: int = TARGET_BYTES,
        interval_seconds: float = INTERVAL_SECONDS,
        sweep_interval_seconds: float = SWEEP_INTERVAL_SECONDS,
    ) -> None:
        """Bind the compactor.

        Args:
            engine: Engine whose schema carries ``agent_steps``, ``tree_objects`` and ``storage_packs``.
            objects: The store that holds the packs.
            delete_object: Deletes one object by key; a missing object is not an error.
            min_age_seconds: A pack is merged only when it is older than this.
            grace_seconds: A pack that no row points at is deleted only when it was retired longer ago than this.
            target_bytes: A pack over half of this size is not a merge candidate. A pass skips a
                candidate that would take the new pack past this size, and goes on to the next one.
            interval_seconds: The wait between two passes of the background thread.
            sweep_interval_seconds: The least time between two sweeps of the packs with no row.
        """
        self._engine = engine
        self._objects = objects
        self._delete_object = delete_object
        self._min_age = timedelta(seconds=min_age_seconds)
        self._grace_seconds = grace_seconds
        self._target_bytes = target_bytes
        self._interval_seconds = interval_seconds
        self._sweep_interval_seconds = sweep_interval_seconds
        self._next_sweep = 0.0
        self._stop_event = threading.Event()
        self._thread: threading.Thread | None = None

    def run_once(self) -> tuple[int, int]:
        """Run one pass: delete the retired packs and the new packs with no row, then merge.

        Each step runs on its own, so a failed step does not skip the others.

        Returns:
            The number of packs merged and the number of packs deleted.
        """
        removed = self._attempt(self.remove_retired)
        if time.monotonic() >= self._next_sweep:
            # Set before the sweep, so a failed sweep also waits the interval and does not LIST each pass.
            self._next_sweep = time.monotonic() + self._sweep_interval_seconds
            removed += self._attempt(self.remove_unrecorded)
        return self._attempt(self.merge), removed

    def _attempt(self, step: Callable[[], int]) -> int:
        """Run one step of a pass, and log a failure instead of raising it.

        Args:
            step: The step.

        Returns:
            The step's count, or ``0`` when it failed.
        """
        try:
            return step()
        except Exception:
            logger.warning("Tree compaction step %s failed; the next pass tries again", step.__name__, exc_info=True)
            return 0

    def merge(self) -> int:
        """Merge the oldest small packs into one new pack, and move their rows to it in one transaction.

        A pack that ``needs_rewrite`` marks comes first, and is rewritten even when it is the only pack.

        Returns:
            The number of old packs merged; ``0`` when fewer than two packs qualify and none is marked,
            when missing, short or empty (broken) objects leave no pack that is worth a pass, when another transaction
            holds a lock that the move needs, or when another pass moved or a delete removed a row first.

        Raises:
            TimeoutError: When half the grace time passed before the commit; the new pack is deleted.
        """
        with Session(self._engine) as session:
            candidates = session.execute(
                select(StoragePackModel.pack_key, StoragePackModel.byte_size, StoragePackModel.needs_rewrite)
                .where(
                    StoragePackModel.created_at < datetime.now(UTC) - self._min_age,
                    StoragePackModel.byte_size != BROKEN_BYTE_SIZE,
                    REFERENCED,
                    or_(
                        # An entry with no row (account deletion, stale step, duplicate, gone job) left dead
                        # bytes in the pack: it is rewritten at any size.
                        StoragePackModel.needs_rewrite,
                        # Over half the target is full: any two candidates fit in one pass.
                        and_(
                            StoragePackModel.byte_size * 2 <= self._target_bytes,
                            StoragePackModel.worker_id != FULL_COMPACTOR_ID,
                        ),
                    ),
                )
                .order_by(StoragePackModel.needs_rewrite.desc(), StoragePackModel.created_at, StoragePackModel.pack_key)
                .limit(MAX_PACKS_PER_PASS)
            ).all()
            rewrite = {key for key, _, marked in candidates if marked}
            keys: list[str] = []
            steps: list[Any] = []
            tree_objects: list[Any] = []
            total = row_count = 0
            # Rows are read one pack at a time, so a pack that does not fit uses none of the byte or row budget.
            for key, size, _ in candidates:
                if row_count >= MAX_ROWS_PER_PASS:
                    break
                # The new pack is built in memory, so it never grows past the target. Each unmarked
                # candidate is at most half the target, so the first two always fit. A marked pack can be
                # larger; as the first pack of a pass it is taken at any size, so it never waits for room.
                if keys and total + size > self._target_bytes:
                    continue
                pack_steps = session.execute(
                    select(
                        _STEPS.c.step_id, _STEPS.c.run_id, _STEPS.c.pack_key, _STEPS.c.pack_offset, _STEPS.c.pack_length
                    ).where(_STEPS.c.pack_key == key)
                ).all()
                pack_objects = session.execute(
                    select(
                        _OBJECTS.c.username,
                        _OBJECTS.c.object_id,
                        _OBJECTS.c.pack_key,
                        _OBJECTS.c.pack_offset,
                        _OBJECTS.c.pack_length,
                    ).where(_OBJECTS.c.pack_key == key)
                ).all()
                rows = len(pack_steps) + len(pack_objects)
                if rows and not keys and key in rewrite and rows >= MAX_ROWS_PER_PASS:
                    # A marked pack never fits under the cap at this size, so it moves in parts, alone. Rows
                    # still point at it, so it stays marked and is not retired until the last part moves.
                    part = sorted([*pack_steps, *pack_objects], key=lambda row: row.pack_offset)
                    part = part[: MAX_ROWS_PER_PASS - 1]
                    keys.append(key)
                    steps += [row for row in part if "step_id" in row._fields]
                    tree_objects += [row for row in part if "step_id" not in row._fields]
                    break
                # No rows: another pass merged the pack, or a delete removed its rows, after the candidate read.
                # A pack at the row cap is full.
                if rows == 0 or rows >= MAX_ROWS_PER_PASS or row_count + rows > MAX_ROWS_PER_PASS:
                    continue
                keys.append(key)
                steps += pack_steps
                tree_objects += pack_objects
                total += size
                row_count += rows
            if not self._worth_a_pass(keys, rewrite):
                return 0
        data = bytearray()
        step_moves: list[tuple[Any, int]] = []
        object_moves: list[tuple[Any, int]] = []
        broken: set[str] = set()
        pack_key, pack = None, b""
        starts = (0, 0, 0)
        # Sorted by pack, so each old pack is read once and only one is in memory at a time.
        for row in sorted([*steps, *tree_objects], key=lambda row: (row.pack_key, row.pack_offset)):
            if row.pack_key in broken:
                continue
            if row.pack_key != pack_key:
                pack_key = row.pack_key
                starts = (len(data), len(step_moves), len(object_moves))
                pack = b""
                try:
                    pack = self._objects.get(pack_key)
                except KeyError:
                    logger.exception("Pack %s is missing; its rows stay on it and it leaves the candidates", pack_key)
                    broken.add(pack_key)
                    continue
                except Exception as error:
                    response = getattr(error, "response", None)
                    code = response.get("Error", {}).get("Code") if isinstance(response, dict) else None
                    if code != _INVALID_RANGE:
                        raise
                    # The S3 store always sends a Range header, and S3 answers 416 for an empty object.
                    # The pack then stays empty, so the short-pack check below marks it broken.
            piece = pack[row.pack_offset : row.pack_offset + row.pack_length]
            # A short piece keeps its row's pack_length, so its row would read the next row's bytes.
            # All rows of the pack leave the pass, also the ones already copied.
            if len(piece) != row.pack_length:
                logger.error(
                    "Pack %s is shorter than its rows; its rows stay on it and it leaves the candidates", pack_key
                )
                broken.add(pack_key)
                del data[starts[0] :], step_moves[starts[1] :], object_moves[starts[2] :]
                continue
            (step_moves if "step_id" in row._fields else object_moves).append((row, len(data)))
            data += piece
        del pack
        if broken:
            self._mark_broken(broken)
            if broken & rewrite:
                logger.error(
                    "Packs %s have needs_rewrite but are broken: they leave the candidates, and their bytes "
                    "that no row points at stay until someone repairs them",
                    ", ".join(sorted(broken & rewrite)),
                )
            keys = [key for key in keys if key not in broken]
            if not self._worth_a_pass(keys, rewrite):
                return 0
        # The new key is made after the GETs, so slow GETs do not use the half-grace limit, and its time
        # is not older than the PUT.
        key_ns = time.time_ns()
        new_key = f"{COMPACTED_PREFIX}/{key_ns:020d}-{uuid4().hex}.pack"
        self._objects.put(new_key, data)
        byte_size = len(data)
        del data
        with Session(self._engine) as session:
            moved = self._lock_and_move(session, keys, step_moves, object_moves, new_key)
            expected = len(step_moves) + len(object_moves)
            if moved is None:
                logger.warning(
                    "Tree compaction gave up a pass of %d packs and %d rows: another transaction holds a lock "
                    "that the move needs; the next pass tries again",
                    len(keys),
                    expected,
                )
            elif moved != expected:
                logger.warning(
                    "Tree compaction gave up a pass of %d packs and %d rows: another pass moved or a delete "
                    "removed %d of the rows first; the next pass tries again",
                    len(keys),
                    expected,
                    expected - moved,
                )
            if moved != expected:
                session.rollback()
                self._discard(new_key)
                return 0
            # The retire time starts the grace time of the old packs, so it is the commit time.
            retired_at = datetime.now(UTC)
            worker_id = FULL_COMPACTOR_ID if 2 * moved > MAX_ROWS_PER_PASS else COMPACTOR_ID
            session.add(
                StoragePackModel(pack_key=new_key, worker_id=worker_id, byte_size=byte_size, created_at=retired_at)
            )
            for chunk in _chunks(keys):
                session.execute(
                    update(StoragePackModel).where(StoragePackModel.pack_key.in_(chunk)).values(created_at=retired_at)
                )
            # Another replica deletes a new pack with no row once its key time is older than the grace time.
            if time.time_ns() - key_ns > self._grace_seconds * 1e9 / 2:
                session.rollback()
                self._discard(new_key)
                raise TimeoutError(f"the merge into {new_key} took more than half the grace time")
            session.commit()
        return len(keys)

    @staticmethod
    def _worth_a_pass(keys: list[str], rewrite: set[str]) -> bool:
        """Return whether the packs of a pass make a new pack: two or more, or one that must be rewritten.

        Args:
            keys: The pack keys of the pass.
            rewrite: The candidate keys that ``needs_rewrite`` marks.

        Returns:
            Whether the pass goes on.
        """
        return len(keys) >= 2 or any(key in rewrite for key in keys)

    def _lock_and_move(
        self,
        session: Session,
        keys: list[str],
        step_moves: list[tuple[Any, int]],
        object_moves: list[tuple[Any, int]],
        new_key: str,
    ) -> int | None:
        """Lock the jobs of the moved steps and the old packs, then point the rows at the new pack.

        Args:
            session: The merge transaction.
            keys: The old pack keys.
            step_moves: Each step row with its offset in the new pack.
            object_moves: Each tree object row with its offset in the new pack.
            new_key: The new pack key.

        Returns:
            The number of rows moved; ``None`` when another transaction holds one of the locks.
        """
        run_ids = sorted({row.run_id for row, _ in step_moves})
        # Each lock is taken in one order and with NOWAIT, so a pass never waits and two passes over
        # shared packs cannot each hold a part. The jobs come first, as in StepLogStore._apply. KEY SHARE
        # keeps a job delete, whose cascade locks steps in another order, out until the commit. It also
        # blocks SELECT ... FOR UPDATE on the job (a claim skips the row), but not a plain UPDATE. The
        # row cap keeps that wait short; the half-grace check only stops a late commit.
        try:
            for chunk in _chunks(run_ids):
                session.execute(
                    select(JobModel.optimization_id)
                    .where(JobModel.optimization_id.in_(chunk))
                    .order_by(JobModel.optimization_id)
                    .with_for_update(read=True, key_share=True, nowait=True)
                ).all()
            for chunk in _chunks(sorted(keys)):
                session.execute(
                    select(StoragePackModel.pack_key)
                    .where(StoragePackModel.pack_key.in_(chunk))
                    .order_by(StoragePackModel.pack_key)
                    .with_for_update(nowait=True)
                ).all()
        except OperationalError as error:
            if getattr(error.orig, "pgcode", None) != _LOCK_NOT_AVAILABLE:
                raise
            return None
        moved = 0
        for chunk in _chunks(step_moves):
            moved += session.execute(_move_chunk(_STEPS, ("step_id",), chunk, new_key)).rowcount
        for chunk in _chunks(object_moves):
            moved += session.execute(_move_chunk(_OBJECTS, ("username", "object_id"), chunk, new_key)).rowcount
        return moved

    def _mark_broken(self, keys: set[str]) -> None:
        """Take packs whose object is missing or short out of the candidates; their rows stay where they are.

        Args:
            keys: The pack keys.
        """
        with Session(self._engine) as session:
            # A plain UPDATE of many rows waits on their locks in no fixed order, and an account purge
            # holds pack locks in key order, so the two could deadlock. The rows are locked in key order
            # with NOWAIT, as the merge does: the compactor never makes a purge wait, and a broken pack
            # that is not marked stays out of this pass and is found broken again by the next one.
            try:
                for chunk in _chunks(sorted(keys)):
                    session.execute(
                        select(StoragePackModel.pack_key)
                        .where(StoragePackModel.pack_key.in_(chunk))
                        .order_by(StoragePackModel.pack_key)
                        .with_for_update(nowait=True)
                    ).all()
            except OperationalError as error:
                if getattr(error.orig, "pgcode", None) != _LOCK_NOT_AVAILABLE:
                    raise
                logger.info("Another transaction holds a broken pack; the next pass marks it")
                return
            for chunk in _chunks(sorted(keys)):
                session.execute(
                    update(StoragePackModel)
                    .where(StoragePackModel.pack_key.in_(chunk))
                    .values(byte_size=BROKEN_BYTE_SIZE)
                )
            session.commit()

    def _discard(self, key: str) -> None:
        """Delete a new pack that no row points at; on a failure, a later pass deletes it.

        Args:
            key: The new pack key.
        """
        try:
            self._delete_object(key)
        except Exception:
            logger.warning("Could not delete the new pack %s; a later pass deletes it", key, exc_info=True)

    def remove_retired(self) -> int:
        """Delete the packs that no row points at and that were retired longer ago than the grace time.

        Returns:
            The number of packs deleted.
        """
        retired = and_(
            StoragePackModel.created_at < datetime.now(UTC) - timedelta(seconds=self._grace_seconds), ~REFERENCED
        )
        # No lock: no new row ever points at a retired pack, and both deletes are idempotent.
        with Session(self._engine) as session:
            keys = list(
                session.scalars(
                    select(StoragePackModel.pack_key)
                    .where(retired)
                    .order_by(StoragePackModel.created_at, StoragePackModel.pack_key)
                    .limit(MAX_PACKS_PER_PASS)
                )
            )
        # The object goes before its row: recovery applies a pack object that has no row.
        deleted: list[str] = []
        for key in keys:
            try:
                self._delete_object(key)
            except Exception:
                logger.warning("Could not delete the retired pack %s; a later pass tries again", key, exc_info=True)
                continue
            deleted.append(key)
        if deleted:
            with Session(self._engine) as session:
                for chunk in _chunks(deleted):
                    session.execute(delete(StoragePackModel).where(StoragePackModel.pack_key.in_(chunk), retired))
                session.commit()
        return len(deleted)

    def remove_unrecorded(self) -> int:
        """Delete the packs with no ``storage_packs`` row that no writer applies any more.

        On PostgreSQL only one replica sweeps at a time: a replica that does not get the advisory lock skips.

        Returns:
            The number of packs deleted; ``0`` when another replica holds the sweep lock.
        """
        if self._engine.dialect.name != "postgresql":
            return self._sweep_unrecorded()
        # The transaction-level lock goes with the connection's transaction, also when the sweep raises.
        with self._engine.connect() as connection, connection.begin():
            if not connection.scalar(text("SELECT pg_try_advisory_xact_lock(:key)"), {"key": SWEEP_LOCK_KEY}):
                logger.info("Another replica sweeps the tree packs with no row; this pass skips the sweep")
                return 0
            return self._sweep_unrecorded()

    def _sweep_unrecorded(self) -> int:
        """List both pack prefixes and delete the old packs with no row.

        A new pack that a pass wrote and did not commit goes once it is older than the grace time. A
        flush pack goes once it is older than ``UNRECORDED_FLUSH_PACK_AGE_SECONDS``, and only after this
        sweep claims it.

        Returns:
            The number of packs deleted.
        """
        now_ns = time.time_ns()
        old: dict[str, list[str]] = {}
        for prefix, age_seconds in (
            (COMPACTED_PREFIX, self._grace_seconds),
            (PACK_PREFIX, UNRECORDED_FLUSH_PACK_AGE_SECONDS),
        ):
            # List before the row read: a pack that commits between the two has its row in the read.
            # The whole prefix is listed each pass: ObjectStore.list has no start-after option.
            listed = self._objects.list(f"{prefix}/")
            # The key starts with its write time, so a writer that is still before its commit keeps its pack.
            cutoff_ns = now_ns - int(age_seconds * 1e9)
            old[prefix] = []
            for key in listed:
                try:
                    written_ns = int(key.rsplit("/", 1)[-1][:20])
                except ValueError:
                    logger.warning("Skipping %s: its key does not start with a write time", key)
                    continue
                if written_ns < cutoff_ns:
                    old[prefix].append(key)
        if not old[COMPACTED_PREFIX] and not old[PACK_PREFIX]:
            return 0
        recorded: set[str] = set()
        claimed: list[str] = []
        with Session(self._engine) as session:
            for chunk in _chunks(old[COMPACTED_PREFIX]):
                recorded.update(
                    session.scalars(select(StoragePackModel.pack_key).where(StoragePackModel.pack_key.in_(chunk)))
                )
            claimed = self._claim_flush_packs(session, old[PACK_PREFIX])
            session.commit()
        removed = 0
        for key in [key for key in old[COMPACTED_PREFIX] if key not in recorded] + claimed:
            try:
                self._delete_object(key)
            except Exception:
                # A claimed flush pack keeps its claim row, so remove_retired deletes it after the grace time.
                logger.warning("Could not delete the unrecorded pack %s; a later pass tries again", key, exc_info=True)
                continue
            removed += 1
        return removed

    def _claim_flush_packs(self, session: Session, keys: list[str]) -> list[str]:
        """Insert a claim row for each flush pack that has no ``storage_packs`` row yet.

        The claim is the insert that :meth:`StepLogStore._apply` makes, so a recovery that applies the
        pack and this sweep cannot both win. The row has the pack's worker id, so the worker's next
        recovery counts the pack as recorded and does not GET it.

        Args:
            session: The open sweep transaction; the caller commits.
            keys: The old flush pack keys, ``packs/<worker>/<time>-<id>.pack``.

        Returns:
            The keys that this sweep claimed; a pack that an apply recorded first is not one of them.
        """
        recorded: set[str] = set()
        for chunk in _chunks(keys):
            recorded.update(
                session.scalars(select(StoragePackModel.pack_key).where(StoragePackModel.pack_key.in_(chunk)))
            )
        # Most old flush packs have a row; only the rest get a claim, so the sweep's writes do not grow
        # with the stored data. The insert still has ON CONFLICT: an apply can record a pack after the read.
        insert = postgres_insert if self._engine.dialect.name == "postgresql" else sqlite_insert
        now = datetime.now(UTC)
        claimed: list[str] = []
        for chunk in _chunks(sorted(key for key in keys if key not in recorded)):
            claimed.extend(
                session.scalars(
                    insert(StoragePackModel)
                    .values(
                        [
                            {
                                "pack_key": key,
                                "worker_id": key.split("/")[1],
                                "byte_size": CLAIM_BYTE_SIZE,
                                "created_at": now,
                            }
                            for key in chunk
                        ]
                    )
                    .on_conflict_do_nothing()
                    .returning(StoragePackModel.pack_key)
                )
            )
        return sorted(claimed)

    def start(self) -> None:
        """Start the background pass loop."""
        self._thread = threading.Thread(target=self._run, name="tree-compactor", daemon=True)
        self._thread.start()

    def stop(self) -> None:
        """Stop the background pass loop; a pass cut short loses nothing."""
        self._stop_event.set()
        if self._thread is not None:
            self._thread.join(timeout=2.0)

    def _run(self) -> None:
        """Run one pass each interval until stopped."""
        while not self._stop_event.wait(self._interval_seconds):
            self.run_once()


def start_tree_compactor(engine: Engine) -> TreeCompactor | None:
    """Start the compactor over the S3 bucket that ``SKYNET_TREE_BUCKET`` names.

    Args:
        engine: The shared database engine.

    Returns:
        The started compactor, or ``None`` when no bucket is set.

    Raises:
        RuntimeError: When a bucket is set and boto3 is not installed.
    """
    found = tree_bucket_client()
    if found is None:
        return None
    bucket, client = found

    def delete_object(key: str) -> None:
        """Delete one object; S3 does not fail on a missing key.

        Args:
            key: The object key.
        """
        client.delete_object(Bucket=bucket, Key=key)

    compactor = TreeCompactor(engine, S3ObjectStore(bucket, client), delete_object)
    compactor.start()
    return compactor
