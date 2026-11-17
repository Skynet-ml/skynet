"""Lazy per-workspace data migration on ``workspaces.storage_version`` (SKY-713).

Each workspace names the format of its data in ``storage_version``. When a
worker wakes a workspace, :func:`ensure_current` upgrades that workspace's
data to :func:`current_version` before any other work, so a format change
ships without a global stop-the-world migration. Alembic still owns the
PostgreSQL DDL; this module only changes data.

Version 1 is the step log and tree format of SKY-710. ``UPGRADES`` is the
ordered registry: ``UPGRADES[i]`` takes a workspace from version
``BASE_VERSION + i`` to the next one. Append new steps at the end and never
reorder or remove one.

Each step runs in its own transaction that holds the workspace row lock
(``SELECT ... FOR UPDATE``) and sets the new version in that transaction, so
two workers that wake the same workspace run each step once. A step's
database writes roll back with a failed step. Writes outside the database
(for example object store PUTs) do not, so a step must be safe to run again.
SQLite has no row locks; only PostgreSQL gives the once-only guarantee.

A step must not commit or roll back the session it gets. :func:`ensure_current`
ends that transaction, and the row lock must hold until the version update.
"""

from __future__ import annotations

from collections.abc import Callable
from datetime import UTC, datetime

from sqlalchemy import select, update
from sqlalchemy.engine import Engine
from sqlalchemy.orm import Session

from .models import WorkspaceModel

BASE_VERSION = 1
UpgradeStep = Callable[[Session, str], None]
UPGRADES: tuple[UpgradeStep, ...] = ()


class StorageUpgradeError(RuntimeError):
    """An upgrade step failed.

    The workspace normally stays at ``from_version``. If the commit landed but its reply was lost,
    the stored version can be ahead of ``from_version``.
    """

    def __init__(self, workspace_id: str, from_version: int) -> None:
        """Name the workspace and the version whose upgrade failed.

        Args:
            workspace_id: The workspace.
            from_version: The version that the failed upgrade started from.
        """
        super().__init__(f"workspace {workspace_id}: the upgrade from storage version {from_version} failed")
        self.workspace_id = workspace_id
        self.from_version = from_version


class StorageVersionAheadError(RuntimeError):
    """The workspace's data uses a newer format than this code knows."""

    def __init__(self, workspace_id: str, stored_version: int) -> None:
        """Name the workspace and its stored version.

        Args:
            workspace_id: The workspace.
            stored_version: The version in the database.
        """
        super().__init__(
            f"workspace {workspace_id}: storage version {stored_version} is newer than {current_version()}"
        )
        self.workspace_id = workspace_id
        self.stored_version = stored_version


class StorageVersionBelowBaseError(RuntimeError):
    """The workspace's stored version is below ``BASE_VERSION``, so no upgrade step fits it."""

    def __init__(self, workspace_id: str, stored_version: int) -> None:
        """Name the workspace and its stored version.

        Args:
            workspace_id: The workspace.
            stored_version: The version in the database.
        """
        super().__init__(
            f"workspace {workspace_id}: storage version {stored_version} is below the base version {BASE_VERSION}"
        )
        self.workspace_id = workspace_id
        self.stored_version = stored_version


def current_version() -> int:
    """Return the storage version that this code writes.

    Returns:
        ``BASE_VERSION`` plus the number of registered upgrade steps.
    """
    return BASE_VERSION + len(UPGRADES)


def _stored_version(session: Session, workspace_id: str, *, lock: bool) -> int:
    """Return a workspace's ``storage_version``, with the row lock when asked.

    Args:
        session: The open session.
        workspace_id: The workspace.
        lock: Whether to take the row lock until the transaction ends.

    Returns:
        The stored version.

    Raises:
        LookupError: The workspace does not exist.
        StorageVersionBelowBaseError: The stored version is below ``BASE_VERSION``.
    """
    query = select(WorkspaceModel.storage_version).where(WorkspaceModel.id == workspace_id)
    if lock:
        query = query.with_for_update()
    version = session.execute(query).scalar_one_or_none()
    if version is None:
        raise LookupError(f"workspace {workspace_id} does not exist")
    # A version below the base would index UPGRADES with a negative number and run the wrong step.
    if version < BASE_VERSION:
        raise StorageVersionBelowBaseError(workspace_id, version)
    return version


def ensure_current(engine: Engine, workspace_id: str) -> int:
    """Upgrade a workspace's data to the current storage version.

    Args:
        engine: The database.
        workspace_id: The workspace to upgrade.

    Returns:
        The workspace's storage version after the call, the current version.

    Raises:
        LookupError: The workspace does not exist.
        StorageUpgradeError: A step, its version update or its commit failed; its cause is that error.
            The stored version normally stays at ``from_version``; after a lost commit reply it can be ahead.
        StorageVersionAheadError: Newer code already upgraded the data.
        StorageVersionBelowBaseError: The stored version is below ``BASE_VERSION``.
    """
    target = current_version()
    # A read without the lock keeps the common wake, a current workspace, free of row lock waits.
    with Session(engine) as session:
        version = _stored_version(session, workspace_id, lock=False)
    while True:
        if version > target:
            raise StorageVersionAheadError(workspace_id, version)
        if version == target:
            return version
        # Closing the session without a commit rolls back and releases the row lock.
        with Session(engine) as session:
            version = _stored_version(session, workspace_id, lock=True)
            if version >= target:
                continue
            # A step's unflushed ORM writes reach the database only at the version update or the commit,
            # so both sit in the try: their errors are the step's errors.
            try:
                UPGRADES[version - BASE_VERSION](session, workspace_id)
                session.execute(
                    update(WorkspaceModel)
                    .where(WorkspaceModel.id == workspace_id)
                    .values(storage_version=version + 1, updated_at=datetime.now(UTC))
                )
                session.commit()
            except Exception as error:
                raise StorageUpgradeError(workspace_id, version) from error
            version += 1
