"""The tree store compactor: merge small step log packs into larger ones (SKY-716).

Each step log flush writes one small pack. A pass of the compactor takes the
oldest small packs that have a ``storage_packs`` row, copies the entries
that rows still point at into one new pack under ``compacted/``, and in one
transaction points every ``agent_steps`` and ``tree_objects`` row at the new
pack. It never deletes a tree or a step: only old pack objects go.

The order makes a kill at any point safe:

1. The new pack is written before the transaction. A kill before the commit
   leaves the rows on the old packs and a new pack with no row, which a
   later pass deletes.
2. The transaction moves the rows, adds the new pack's ``storage_packs`` row,
   and sets each old pack's ``created_at`` to the time it was retired.
3. A later pass deletes a pack that no row points at only when its
   ``created_at`` is older than the grace time, so a reader that read a row
   before the move can still read the old pack. It deletes the object
   first and the ``storage_packs`` row last.

Only packs with a ``storage_packs`` row are merged, so a pack that a flush
wrote but did not apply yet stays for :meth:`StepLogStore.recover`. The new
packs are outside ``packs/``, where recovery never lists them, and their
rows commit in the same transaction as the moved rows.
"""

from __future__ import annotations

import logging
import threading
import time
from collections.abc import Callable
from datetime import UTC, datetime, timedelta
from typing import Any
from uuid import uuid4

from sqlalchemy import bindparam, delete, exists, or_, select, update
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

        Returns:
            The number of packs merged and the number of packs deleted.
        """
        removed = self.remove_retired() + self.remove_unrecorded()
        return self.merge(), removed

    def merge(self) -> int:
        """Merge the oldest small packs into one new pack, and move their rows to it in one transaction.

        Returns:
            The number of old packs merged; ``0`` when fewer than two packs qualify.
        """
        now = datetime.now(UTC)
        with Session(self._engine) as session:
            # SKIP LOCKED gives two replicas different packs; the locks last until the commit.
            candidates = session.execute(
                select(StoragePackModel.pack_key, StoragePackModel.byte_size)
                .where(
                    StoragePackModel.created_at < now - self._min_age,
                    StoragePackModel.byte_size < self._target_bytes,
                    _REFERENCED,
                )
                .order_by(StoragePackModel.created_at, StoragePackModel.pack_key)
                .limit(MAX_PACKS_PER_PASS)
                .with_for_update(skip_locked=True, of=StoragePackModel)
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
            packs = {key: self._objects.get(key) for key in keys}
            new_key = f"{COMPACTED_PREFIX}/{time.time_ns():020d}-{uuid4().hex}.pack"
            parts: list[bytes] = []
            offset = 0
            step_moves: list[dict[str, Any]] = []
            object_moves: list[dict[str, Any]] = []
            for row in [*steps, *tree_objects]:
                parts.append(packs[row.pack_key][row.pack_offset : row.pack_offset + row.pack_length])
                move = {"b_old_key": row.pack_key, "b_new_key": new_key, "b_new_offset": offset}
                if "step_id" in row._fields:
                    step_moves.append({**move, "b_step_id": row.step_id})
                else:
                    object_moves.append({**move, "b_username": row.username, "b_object_id": row.object_id})
                offset += row.pack_length
            data = b"".join(parts)
            self._objects.put(new_key, data)
            session.add(StoragePackModel(pack_key=new_key, worker_id=COMPACTOR_ID, byte_size=len(data), created_at=now))
            # The old pack key in each WHERE leaves a row that another pass moved first where it is.
            if step_moves:
                session.execute(_MOVE_STEP, step_moves)
            if object_moves:
                session.execute(_MOVE_OBJECT, object_moves)
            # The retire time starts the grace time of the old packs.
            session.execute(update(StoragePackModel).where(StoragePackModel.pack_key.in_(keys)).values(created_at=now))
            session.commit()
        return len(keys)

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
        if not listed:
            return 0
        with Session(self._engine) as session:
            recorded = set(
                session.scalars(select(StoragePackModel.pack_key).where(StoragePackModel.pack_key.in_(listed)))
            )
        # The key starts with its write time, so a pass that is still before its commit keeps its new pack.
        cutoff_ns = time.time_ns() - int(self._grace_seconds * 1e9)
        start = len(COMPACTED_PREFIX) + 1
        unrecorded = [key for key in listed if key not in recorded and int(key[start : start + 20]) < cutoff_ns]
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
            try:
                self.run_once()
            except Exception:
                logger.warning("Tree compaction pass failed; the next pass tries again", exc_info=True)


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
