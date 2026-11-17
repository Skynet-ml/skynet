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
2. A short transaction locks the old packs with SKIP LOCKED and moves each
   row only while it still points at its old pack. When a pack is locked by
   another pass or a row moved, the pass rolls back and deletes its new pack.
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
wrote but did not apply yet stays for :meth:`StepLogStore.recover`. The new
packs are outside ``packs/``, where recovery never lists them, and their
rows commit in the same transaction as the moved rows.

A compacted pack under ``target_bytes`` is a candidate again, so a byte can
be copied in more than one pass before its pack reaches the target. This is
acceptable: a pass copies at most ``target_bytes`` and runs at most once an
interval on each replica, and a pack leaves the candidates once it reaches
the target. Size tiers are a later change if the copy volume shows a need.
"""

from __future__ import annotations

import logging
import threading
import time
from collections.abc import Callable
from datetime import UTC, datetime, timedelta
from typing import Any
from uuid import uuid4

from sqlalchemy import bindparam, delete, exists, func, or_, select, update
from sqlalchemy.engine import Engine
from sqlalchemy.orm import Session

from ..config import settings
from .base import ObjectStore
from .models import AgentStepModel, StoragePackModel, TreeObjectModel
from .object_store import S3ObjectStore

try:
    import boto3
except ImportError:
    boto3 = None

logger = logging.getLogger(__name__)

COMPACTED_PREFIX = "compacted"
COMPACTOR_ID = "compactor"
MIN_AGE_SECONDS = 600.0
# Much longer than a read of a row and then of its pack range.
GRACE_SECONDS = 600.0
TARGET_BYTES = 64 * 1024 * 1024
MAX_PACKS_PER_PASS = 1000
INTERVAL_SECONDS = 60.0

_REFERENCED = or_(
    exists().where(AgentStepModel.pack_key == StoragePackModel.pack_key),
    exists().where(TreeObjectModel.pack_key == StoragePackModel.pack_key),
)
_STEPS = AgentStepModel.__table__
_OBJECTS = TreeObjectModel.__table__
# Bind names must differ from the column names: SQLAlchemy keeps those for the SET clause.
_MOVE_STEP = (
    update(_STEPS)
    .where(_STEPS.c.step_id == bindparam("b_step_id"), _STEPS.c.pack_key == bindparam("b_old_key"))
    .values(pack_key=bindparam("b_new_key"), pack_offset=bindparam("b_new_offset"))
)
_MOVE_OBJECT = (
    update(_OBJECTS)
    .where(
        _OBJECTS.c.username == bindparam("b_username"),
        _OBJECTS.c.object_id == bindparam("b_object_id"),
        _OBJECTS.c.pack_key == bindparam("b_old_key"),
    )
    .values(pack_key=bindparam("b_new_key"), pack_offset=bindparam("b_new_offset"))
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
    ) -> None:
        """Bind the compactor.

        Args:
            engine: Engine whose schema carries ``agent_steps``, ``tree_objects`` and ``storage_packs``.
            objects: The store that holds the packs.
            delete_object: Deletes one object by key; a missing object is not an error.
            min_age_seconds: A pack is merged only when it is older than this.
            grace_seconds: A pack that no row points at is deleted only when it was retired longer ago than this.
            target_bytes: A pack of this size or more is not merged; a pass stops adding packs at this size.
            interval_seconds: The wait between two passes of the background thread.
        """
        self._engine = engine
        self._objects = objects
        self._delete_object = delete_object
        self._min_age = timedelta(seconds=min_age_seconds)
        self._grace_seconds = grace_seconds
        self._target_bytes = target_bytes
        self._interval_seconds = interval_seconds
        self._stop_event = threading.Event()
        self._thread: threading.Thread | None = None

    def run_once(self) -> tuple[int, int]:
        """Run one pass: delete the retired packs and the new packs with no row, then merge.

        Each step runs on its own, so a failed step does not skip the others.

        Returns:
            The number of packs merged and the number of packs deleted.
        """
        removed = self._attempt(self.remove_retired) + self._attempt(self.remove_unrecorded)
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

        Returns:
            The number of old packs merged; ``0`` when fewer than two packs qualify, or when another
            pass holds or moved one of them.

        Raises:
            TimeoutError: When half the grace time passed before the commit; the new pack is deleted.
        """
        with Session(self._engine) as session:
            candidates = session.execute(
                select(StoragePackModel.pack_key, StoragePackModel.byte_size)
                .where(
                    StoragePackModel.created_at < datetime.now(UTC) - self._min_age,
                    StoragePackModel.byte_size < self._target_bytes,
                    _REFERENCED,
                )
                .order_by(StoragePackModel.created_at, StoragePackModel.pack_key)
                .limit(MAX_PACKS_PER_PASS)
            ).all()
            keys: list[str] = []
            total = 0
            for key, size in candidates:
                if total >= self._target_bytes:
                    break
                keys.append(key)
                total += size
            if len(keys) < 2:
                return 0
            steps = session.execute(
                select(_STEPS.c.step_id, _STEPS.c.pack_key, _STEPS.c.pack_offset, _STEPS.c.pack_length).where(
                    _STEPS.c.pack_key.in_(keys)
                )
            ).all()
            tree_objects = session.execute(
                select(
                    _OBJECTS.c.username,
                    _OBJECTS.c.object_id,
                    _OBJECTS.c.pack_key,
                    _OBJECTS.c.pack_offset,
                    _OBJECTS.c.pack_length,
                ).where(_OBJECTS.c.pack_key.in_(keys))
            ).all()
        # The new key is made only now, so its time is not older than the PUT.
        key_ns = time.time_ns()
        new_key = f"{COMPACTED_PREFIX}/{key_ns:020d}-{uuid4().hex}.pack"
        parts: list[bytes] = []
        offset = 0
        step_moves: list[dict[str, Any]] = []
        object_moves: list[dict[str, Any]] = []
        pack_key, pack = None, b""
        # Sorted by pack, so each old pack is read once and only one is in memory at a time.
        for row in sorted([*steps, *tree_objects], key=lambda row: (row.pack_key, row.pack_offset)):
            if row.pack_key != pack_key:
                pack_key, pack = row.pack_key, self._objects.get(row.pack_key)
            parts.append(pack[row.pack_offset : row.pack_offset + row.pack_length])
            move = {"b_old_key": row.pack_key, "b_new_key": new_key, "b_new_offset": offset}
            if "step_id" in row._fields:
                step_moves.append({**move, "b_step_id": row.step_id})
            else:
                object_moves.append({**move, "b_username": row.username, "b_object_id": row.object_id})
            offset += row.pack_length
        del pack
        data = b"".join(parts)
        del parts
        self._objects.put(new_key, data)
        byte_size = len(data)
        del data
        with Session(self._engine) as session:
            # SKIP LOCKED: a pass that holds one of the packs wins; this pass gives up and does not wait.
            locked = session.scalars(
                select(StoragePackModel.pack_key)
                .where(StoragePackModel.pack_key.in_(keys))
                .with_for_update(skip_locked=True)
            ).all()
            moved = 0
            if len(locked) == len(keys):
                # The old pack key in each WHERE leaves a row that another pass moved first where it is.
                if step_moves:
                    session.execute(_MOVE_STEP, step_moves)
                if object_moves:
                    session.execute(_MOVE_OBJECT, object_moves)
                moved = session.scalar(
                    select(func.count()).select_from(_STEPS).where(_STEPS.c.pack_key == new_key)
                ) + session.scalar(select(func.count()).select_from(_OBJECTS).where(_OBJECTS.c.pack_key == new_key))
            if moved != len(step_moves) + len(object_moves):
                session.rollback()
                self._discard(new_key)
                return 0
            # The retire time starts the grace time of the old packs, so it is the commit time.
            retired_at = datetime.now(UTC)
            session.add(
                StoragePackModel(pack_key=new_key, worker_id=COMPACTOR_ID, byte_size=byte_size, created_at=retired_at)
            )
            session.execute(
                update(StoragePackModel).where(StoragePackModel.pack_key.in_(keys)).values(created_at=retired_at)
            )
            # Another replica deletes a new pack with no row once its key time is older than the grace time.
            if time.time_ns() - key_ns > self._grace_seconds * 1e9 / 2:
                session.rollback()
                self._discard(new_key)
                raise TimeoutError(f"the merge into {new_key} took more than half the grace time")
            session.commit()
        return len(keys)

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
        cutoff = datetime.now(UTC) - timedelta(seconds=self._grace_seconds)
        with Session(self._engine) as session:
            keys = list(
                session.scalars(
                    select(StoragePackModel.pack_key)
                    .where(StoragePackModel.created_at < cutoff, ~_REFERENCED)
                    .order_by(StoragePackModel.created_at, StoragePackModel.pack_key)
                    .limit(MAX_PACKS_PER_PASS)
                    .with_for_update(skip_locked=True, of=StoragePackModel)
                )
            )
            # The object goes before its row: recovery applies a pack object that has no row.
            for key in keys:
                self._delete_object(key)
            if keys:
                session.execute(delete(StoragePackModel).where(StoragePackModel.pack_key.in_(keys)))
                session.commit()
        return len(keys)

    def remove_unrecorded(self) -> int:
        """Delete the new packs that a pass wrote and did not commit, once they are older than the grace time.

        Returns:
            The number of packs deleted.
        """
        # List before the row read: a new pack that commits between the two has its row in the read.
        listed = self._objects.list(f"{COMPACTED_PREFIX}/")
        # The key starts with its write time, so a pass that is still before its commit keeps its new pack.
        cutoff_ns = time.time_ns() - int(self._grace_seconds * 1e9)
        start = len(COMPACTED_PREFIX) + 1
        old = [key for key in listed if int(key[start : start + 20]) < cutoff_ns]
        if not old:
            return 0
        with Session(self._engine) as session:
            recorded = set(session.scalars(select(StoragePackModel.pack_key).where(StoragePackModel.pack_key.in_(old))))
        unrecorded = [key for key in old if key not in recorded]
        for key in unrecorded:
            self._delete_object(key)
        return len(unrecorded)

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
    bucket = settings.tree_bucket
    if not bucket:
        return None
    if boto3 is None:
        raise RuntimeError("boto3 is not installed")
    client = boto3.client("s3")

    def delete_object(key: str) -> None:
        """Delete one object; S3 does not fail on a missing key.

        Args:
            key: The object key.
        """
        client.delete_object(Bucket=bucket, Key=key)

    compactor = TreeCompactor(engine, S3ObjectStore(bucket, client), delete_object)
    compactor.start()
    return compactor
