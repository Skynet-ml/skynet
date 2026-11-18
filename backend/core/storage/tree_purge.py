"""Purge an account's trees and step log when the account is deleted (SKY-733).

:func:`purge_user` runs inside the account deletion transaction. It deletes
the account's ``tree_objects`` and ``workspaces`` rows and the
``agent_steps`` rows of its runs. Then it deletes each pack object that no
row points at any more, and the pack's ``storage_packs`` row.

A pack can also hold entries of other accounts. Such a pack stays, because
their rows still point at it. The deleted account's bytes in it have no row,
so no read can reach them, and the next merge of the compactor does not copy
them (:mod:`core.storage.tree_compactor`).

The S3 delete happens before the transaction commits, so a failed delete
fails the whole account deletion. A rollback after the delete leaves rows
that point at a deleted pack; the next deletion of the account removes them.

The purge is one function so that a later change can add the step log
shards (SKY-731) in one place.
"""

from __future__ import annotations

from collections.abc import Callable, Iterable
from dataclasses import dataclass

from sqlalchemy import delete, exists, or_, select
from sqlalchemy.orm import Session

from ..config import settings
from .models import AgentStepModel, StoragePackModel, TreeObjectModel, WorkspaceModel

try:
    import boto3
except ImportError:
    boto3 = None

# Keys in one IN query, far below the SQLite bind limit.
CHUNK_SIZE = 200

_REFERENCED = or_(
    exists().where(AgentStepModel.pack_key == StoragePackModel.pack_key),
    exists().where(TreeObjectModel.pack_key == StoragePackModel.pack_key),
)


class TreePurgeError(RuntimeError):
    """A pack of the deleted account could not be deleted from the tree bucket."""


@dataclass
class TreePurgeSummary:
    """Outcome of one purge.

    Attributes:
        deleted_rows: ``tree_objects``, ``workspaces`` and ``agent_steps`` rows deleted.
        deleted_packs: Pack objects deleted, with their ``storage_packs`` rows.
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


def bucket_deleter() -> Callable[[str], None] | None:
    """Return a function that deletes one object from the tree bucket.

    Returns:
        The delete function, or ``None`` when ``SKYNET_TREE_BUCKET`` is not set.

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

    return delete_object


def purge_user(
    session: Session,
    username: str,
    run_ids: list[str],
    *,
    delete_object: Callable[[str], None] | None = None,
) -> TreePurgeSummary:
    """Delete an account's tree rows, its runs' steps, and the packs that only they used.

    A second call finds no rows and deletes nothing.

    Args:
        session: The account deletion's open session; the caller commits.
        username: The account's lowercased-email identity.
        run_ids: The job ids of the account. Call this before their jobs go,
            because the jobs' cascade deletes the steps without their pack keys.
        delete_object: Deletes one pack object; a missing object is not an
            error. ``None`` uses the bucket that ``SKYNET_TREE_BUCKET`` names.

    Returns:
        The counts of deleted rows and packs.

    Raises:
        TreePurgeError: When a pack must go and no bucket is set, or its delete fails.
    """
    pack_keys: set[str] = set()
    deleted_rows = 0
    for chunk in _chunks(run_ids):
        steps = session.scalars(
            delete(AgentStepModel).where(AgentStepModel.run_id.in_(chunk)).returning(AgentStepModel.pack_key),
            execution_options={"synchronize_session": False},
        ).all()
        pack_keys.update(steps)
        deleted_rows += len(steps)
    objects = session.scalars(
        delete(TreeObjectModel).where(TreeObjectModel.username == username).returning(TreeObjectModel.pack_key),
        execution_options={"synchronize_session": False},
    ).all()
    pack_keys.update(objects)
    deleted_rows += len(objects)
    workspaces = session.execute(
        delete(WorkspaceModel).where(WorkspaceModel.username == username),
        execution_options={"synchronize_session": False},
    )
    deleted_rows += workspaces.rowcount or 0

    # The lock makes a compactor merge of these packs give up: it locks them with NOWAIT.
    unused: list[str] = []
    for chunk in _chunks(sorted(pack_keys)):
        unused.extend(
            session.scalars(
                select(StoragePackModel.pack_key)
                .where(StoragePackModel.pack_key.in_(chunk), ~_REFERENCED)
                .order_by(StoragePackModel.pack_key)
                .with_for_update()
            )
        )
    if unused:
        deleter = delete_object or bucket_deleter()
        if deleter is None:
            raise TreePurgeError(f"{len(unused)} tree packs must be deleted, but SKYNET_TREE_BUCKET is not set")
        # The object goes before its row: recovery applies a pack object that has no row.
        for key in unused:
            try:
                deleter(key)
            except Exception as exc:
                raise TreePurgeError(f"could not delete the tree pack {key}") from exc
        for chunk in _chunks(unused):
            session.execute(
                delete(StoragePackModel).where(StoragePackModel.pack_key.in_(chunk)),
                execution_options={"synchronize_session": False},
            )
    return TreePurgeSummary(deleted_rows=deleted_rows, deleted_packs=len(unused))
