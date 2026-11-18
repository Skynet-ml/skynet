"""Purge an account's trees and step log when the account is deleted (SKY-733).

:func:`purge_user` runs inside the account deletion transaction. It deletes
the account's ``tree_objects`` and ``workspaces`` rows and the
``agent_steps`` rows of its runs. Then it deletes each pack object that no
row points at any more, and the pack's ``storage_packs`` row.

A pack can also hold entries of other accounts. Such a pack stays, because
their rows still point at it, and no read can reach the deleted account's
bytes in it. The purge sets the pack's ``needs_rewrite`` mark, so the next
pass of the compactor rewrites it, at any size, with only the entries that
rows still point at. The old pack is then retired and deleted, with the
deleted account's bytes (:mod:`core.storage.tree_compactor`).

The locks follow one global order: the account's ``jobs`` rows, then the
``storage_packs`` rows, each in key order, and only then the deletes.
:meth:`StepLogStore._apply` locks the jobs first too, so no flush of the
account's runs writes a row behind the purge. The compactor takes the same
locks with NOWAIT, so a merge of these packs gives up instead of waiting.
A merge that commits between the key read and the pack lock moves rows to a
new pack; the purge then locks again, in key order, under a savepoint, and
never locks a pack after a delete.

Only a pack that a deleted row pointed at can be deleted. A locked pack with
no rows was retired by a merge, and the compactor deletes it after its grace
time, so a reader that read a row before the merge can still read it. For
the same reason, the purge deletes a pack at once only when its
``created_at`` is older than the grace time. A merge that moved only a part
of a pack's rows set ``created_at`` to the move time, and a reader of a
moved row can still GET the old pack inside the grace time. The compactor
deletes a younger pack that no row points at once the grace time ends.

The S3 delete happens before the transaction commits, so a failed delete
fails the whole account deletion. A rollback after the delete leaves rows
that point at a deleted pack; the next deletion of the account removes them,
and S3 does not fail on a key that is already gone.

A run that is deleted before its account goes the same way:
:func:`mark_run_packs` runs in the job delete's transaction, before the
cascade deletes the run's steps, and marks every pack that they point at.

The purge is one function so that a later change can add the step log
shards (SKY-731) in one place.
"""

from __future__ import annotations

from collections.abc import Callable, Iterable
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta

from sqlalchemy import delete, select, update
from sqlalchemy.orm import Session

from .models import AgentStepModel, JobModel, StoragePackModel, TreeObjectModel, WorkspaceModel
from .tree_compactor import GRACE_SECONDS, REFERENCED, tree_bucket_client

# Keys in one IN query, far below the SQLite bind limit.
CHUNK_SIZE = 200
# The most keys that one S3 DeleteObjects request takes.
S3_DELETE_BATCH = 1000
# Pack lock rounds before the purge gives up; each extra round needs a merge that commits in between.
PACK_LOCK_TRIES = 3
# Pack keys that one error message names; the message gives the full count.
ERROR_KEYS_SHOWN = 5


class TreePurgeError(RuntimeError):
    """A pack of the deleted account could not be deleted from the tree bucket."""


@dataclass
class TreePurgeSummary:
    """Outcome of one purge.

    Attributes:
        deleted_rows: ``tree_objects``, ``workspaces`` and ``agent_steps`` rows deleted.
        deleted_packs: Pack objects deleted at once, with their ``storage_packs`` rows. The
            compactor deletes the unused packs that are younger than the grace time.
    """

    deleted_rows: int
    deleted_packs: int


def _chunks(items: list[str]) -> Iterable[list[str]]:
    """Split a list into chunks of at most ``CHUNK_SIZE`` items.

    Args:
        items: The list.

    Returns:
        The chunks, in order.
    """
    return (items[start : start + CHUNK_SIZE] for start in range(0, len(items), CHUNK_SIZE))


def _describe(keys: list[str]) -> str:
    """Name the count of tree packs and the first few keys, for an error message.

    Args:
        keys: The pack keys.

    Returns:
        The text.
    """
    more = ", ..." if len(keys) > ERROR_KEYS_SHOWN else ""
    return f"{len(keys)} tree packs ({', '.join(keys[:ERROR_KEYS_SHOWN])}{more})"


def _lock_jobs(session: Session, run_ids: list[str]) -> None:
    """Lock ``jobs`` rows FOR UPDATE in key order, as :meth:`StepLogStore._apply` does.

    Args:
        session: The open transaction.
        run_ids: The job ids, sorted.
    """
    for chunk in _chunks(run_ids):
        session.execute(
            select(JobModel.optimization_id)
            .where(JobModel.optimization_id.in_(chunk))
            .order_by(JobModel.optimization_id)
            .with_for_update()
        ).all()


def _lock_packs(session: Session, keys: set[str]) -> None:
    """Lock ``storage_packs`` rows FOR UPDATE in key order.

    Args:
        session: The purge transaction.
        keys: The pack keys.
    """
    for chunk in _chunks(sorted(keys)):
        session.execute(
            select(StoragePackModel.pack_key)
            .where(StoragePackModel.pack_key.in_(chunk))
            .order_by(StoragePackModel.pack_key)
            .with_for_update()
        ).all()


def _read_pack_keys(session: Session, username: str, run_ids: list[str]) -> set[str]:
    """Read the keys of the packs that the account's step and tree object rows point at.

    Args:
        session: The purge transaction.
        username: The account.
        run_ids: The account's job ids.

    Returns:
        The pack keys.
    """
    keys: set[str] = set()
    for chunk in _chunks(run_ids):
        keys.update(session.scalars(select(AgentStepModel.pack_key).where(AgentStepModel.run_id.in_(chunk)).distinct()))
    keys.update(
        session.scalars(select(TreeObjectModel.pack_key).where(TreeObjectModel.username == username).distinct())
    )
    return keys


def _lock_account_packs(session: Session, username: str, run_ids: list[str]) -> set[str]:
    """Lock every pack that the account's rows point at, in key order, before any delete.

    A compactor merge can move rows to a new pack between the key read and the lock. The read after
    the lock then finds the new key. The purge rolls back to the savepoint, which releases the locks it
    took there, and locks the union in key order again. It never takes a pack lock out of order.

    Args:
        session: The purge transaction, with the account's jobs locked.
        username: The account.
        run_ids: The account's job ids.

    Returns:
        The keys of the packs that the account's rows point at, all locked. No row moves off them before
        the commit: a merge needs these locks, and a flush of the account's runs needs the job locks.

    Raises:
        TreePurgeError: When the keys still change after ``PACK_LOCK_TRIES`` tries; a retry can succeed.
    """
    keys = _read_pack_keys(session, username, run_ids)
    for _ in range(PACK_LOCK_TRIES):
        savepoint = session.begin_nested()
        _lock_packs(session, keys)
        found = _read_pack_keys(session, username, run_ids)
        if found <= keys:
            savepoint.commit()
            return found
        savepoint.rollback()
        keys |= found
    raise TreePurgeError("the account's tree packs kept moving during the purge; try again")


def bucket_deleter() -> Callable[[list[str]], None] | None:
    """Return a function that deletes objects from the tree bucket in DeleteObjects batches.

    Returns:
        The delete function, or ``None`` when ``SKYNET_TREE_BUCKET`` is not set.

    Raises:
        RuntimeError: When a bucket is set and boto3 is not installed.
    """
    found = tree_bucket_client()
    if found is None:
        return None
    bucket, client = found

    def delete_objects(keys: list[str]) -> None:
        """Delete the objects; S3 does not fail on a missing key.

        Args:
            keys: The object keys.

        Raises:
            TreePurgeError: When S3 reports a key it could not delete.
        """
        for start in range(0, len(keys), S3_DELETE_BATCH):
            batch = keys[start : start + S3_DELETE_BATCH]
            response = client.delete_objects(
                Bucket=bucket, Delete={"Objects": [{"Key": key} for key in batch], "Quiet": True}
            )
            # DeleteObjects answers 200 also when single keys fail; it lists them in Errors.
            failed = sorted(error.get("Key", "?") for error in response.get("Errors") or [])
            if failed:
                raise TreePurgeError(f"could not delete {_describe(failed)}")

    return delete_objects


def purge_user(
    session: Session,
    username: str,
    run_ids: list[str],
    *,
    delete_objects: Callable[[list[str]], None] | None = None,
    grace_seconds: float = GRACE_SECONDS,
) -> TreePurgeSummary:
    """Delete an account's tree rows, its runs' steps, and the packs that only they used.

    Packs that other accounts also use get ``needs_rewrite``. A second call
    finds no rows and deletes nothing.

    Args:
        session: The account deletion's open session; the caller commits.
        username: The account's lowercased-email identity.
        run_ids: The job ids of the account. Call this before their jobs go,
            because the jobs' cascade deletes the steps without their pack keys.
        delete_objects: Deletes pack objects; a missing object is not an
            error. ``None`` uses the bucket that ``SKYNET_TREE_BUCKET`` names.
        grace_seconds: An unused pack is deleted at once only when its
            ``created_at`` is older than this; the compactor deletes the others.

    Returns:
        The counts of deleted rows and packs.

    Raises:
        TreePurgeError: When a pack must go and no bucket is set, or its delete fails.
    """
    run_ids = sorted(run_ids)
    # The jobs come first, in key order, as in StepLogStore._apply: a flush of these runs waits for the
    # commit and then finds no job, so it writes no row behind the purge.
    _lock_jobs(session, run_ids)
    # The packs are locked before any row goes, so a compactor merge of them (NOWAIT) gives up and
    # never holds a pack that the purge waits for.
    pointed = _lock_account_packs(session, username, run_ids)

    deleted_rows = 0
    for chunk in _chunks(run_ids):
        deleted_rows += (
            session.execute(
                delete(AgentStepModel).where(AgentStepModel.run_id.in_(chunk)),
                execution_options={"synchronize_session": False},
            ).rowcount
            or 0
        )
    deleted_rows += (
        session.execute(
            delete(TreeObjectModel).where(TreeObjectModel.username == username),
            execution_options={"synchronize_session": False},
        ).rowcount
        or 0
    )
    workspaces = session.execute(
        delete(WorkspaceModel).where(WorkspaceModel.username == username),
        execution_options={"synchronize_session": False},
    )
    deleted_rows += workspaces.rowcount or 0

    # Only the packs that the deleted rows pointed at: a locked pack that no row points at was
    # retired by a merge, and the compactor deletes it after the grace time.
    unreferenced: list[str] = []
    for chunk in _chunks(sorted(pointed)):
        unreferenced.extend(
            session.scalars(
                select(StoragePackModel.pack_key)
                .where(StoragePackModel.pack_key.in_(chunk), ~REFERENCED)
                .order_by(StoragePackModel.pack_key)
            )
        )
    # A part move sets created_at, and its readers may still GET the old pack until the grace time ends.
    retired_before = datetime.now(UTC) - timedelta(seconds=grace_seconds)
    unused: list[str] = []
    for chunk in _chunks(unreferenced):
        unused.extend(
            session.scalars(
                select(StoragePackModel.pack_key)
                .where(StoragePackModel.pack_key.in_(chunk), StoragePackModel.created_at < retired_before)
                .order_by(StoragePackModel.pack_key)
            )
        )
    for chunk in _chunks(sorted(pointed - set(unreferenced))):
        session.execute(
            update(StoragePackModel).where(StoragePackModel.pack_key.in_(chunk)).values(needs_rewrite=True),
            execution_options={"synchronize_session": False},
        )
    if unused:
        try:
            deleter = delete_objects or bucket_deleter()
        except Exception as exc:
            raise TreePurgeError("could not open the tree bucket") from exc
        if deleter is None:
            raise TreePurgeError(f"{len(unused)} tree packs must be deleted, but SKYNET_TREE_BUCKET is not set")
        # The objects go before their rows: recovery applies a pack object that has no row.
        try:
            deleter(unused)
        except TreePurgeError:
            raise
        except Exception as exc:
            raise TreePurgeError(f"could not delete {_describe(unused)}") from exc
        for chunk in _chunks(unused):
            session.execute(
                delete(StoragePackModel).where(StoragePackModel.pack_key.in_(chunk)),
                execution_options={"synchronize_session": False},
            )
    return TreePurgeSummary(deleted_rows=deleted_rows, deleted_packs=len(unused))


def mark_run_packs(session: Session, run_ids: list[str]) -> None:
    """Mark for rewrite each pack that the steps of runs about to be deleted point at.

    The job delete's cascade removes the steps, but not their bytes in packs that other runs share.
    The mark makes the compactor rewrite such a pack without them. A pack that only these steps used
    has no row left after the delete, and the compactor deletes it after the grace time.

    Call this in the job delete's transaction, before the jobs go. The locks follow the purge's
    order: the jobs, then the packs, each in key order. A flush of these runs waits for the jobs, and a
    merge that would move their steps gives up on the job lock, so no step moves off a read pack.

    Args:
        session: The job delete's open session; the caller commits.
        run_ids: The job ids to be deleted. Duplicates and missing ids are allowed.
    """
    run_ids = sorted(set(run_ids))
    _lock_jobs(session, run_ids)
    keys: set[str] = set()
    for chunk in _chunks(run_ids):
        keys.update(session.scalars(select(AgentStepModel.pack_key).where(AgentStepModel.run_id.in_(chunk)).distinct()))
    _lock_packs(session, keys)
    for chunk in _chunks(sorted(keys)):
        session.execute(
            update(StoragePackModel).where(StoragePackModel.pack_key.in_(chunk)).values(needs_rewrite=True),
            execution_options={"synchronize_session": False},
        )
