"""The workspaces table: one workspace for each user, with its policy and its budget (SKY-717).

The workspace id comes from the username (UUID version 5), so the primary key
keeps one workspace for each user, also when two workers create it at the
same time. This store reads and writes the row, and reads the state of the
workspace's budget and generations with no row lock; waking a workspace and
its lazy data migration are in ``core.worker.actors`` and
``core.storage.storage_version``.
"""

from __future__ import annotations

import uuid
from dataclasses import dataclass
from datetime import UTC, datetime
from typing import Any

from sqlalchemy import select, update
from sqlalchemy.dialects.postgresql import insert as postgres_insert
from sqlalchemy.dialects.sqlite import insert as sqlite_insert
from sqlalchemy.engine import Engine
from sqlalchemy.orm import Session

from .models import ExecutionBudgetModel, JobModel, WorkspaceModel
from .storage_version import current_version

# A fixed namespace: a change gives every user a second workspace.
_WORKSPACE_NAMESPACE = uuid.UUID("3f6c2a1e-8b4d-5c7e-9a0f-1d2e3b4c5a6f")


@dataclass(frozen=True)
class WorkspaceRecord:
    """One workspace row, detached from its session."""

    id: str
    username: str
    policy: dict[str, Any]
    execution_budget_id: str | None
    storage_version: int


@dataclass(frozen=True)
class BudgetAdmission:
    """What an agent reads before a paid step: the budget's state and generation, and the job's generation."""

    state: str
    generation: int
    execution_generation: int | None


def workspace_id_for(username: str) -> str:
    """Return the id of a user's workspace.

    Args:
        username: The owner.

    Returns:
        A UUID version 5 of the username, as text.
    """
    return str(uuid.uuid5(_WORKSPACE_NAMESPACE, username))


def _record(row: WorkspaceModel) -> WorkspaceRecord:
    """Copy a workspace row into a record.

    Args:
        row: The row.

    Returns:
        The record.
    """
    return WorkspaceRecord(
        id=row.id,
        username=row.username,
        policy=dict(row.policy or {}),
        execution_budget_id=row.execution_budget_id,
        storage_version=row.storage_version,
    )


class WorkspaceStore:
    """Read and write the workspaces table."""

    def __init__(self, engine: Engine) -> None:
        """Bind the store to a database.

        Args:
            engine: Engine whose schema carries ``workspaces``.
        """
        self._engine = engine

    def get(self, workspace_id: str) -> WorkspaceRecord:
        """Return one workspace.

        Args:
            workspace_id: The workspace.

        Returns:
            The workspace.

        Raises:
            LookupError: The workspace does not exist.
        """
        with Session(self._engine) as session:
            row = session.get(WorkspaceModel, workspace_id)
            if row is None:
                raise LookupError(f"workspace {workspace_id} does not exist")
            return _record(row)

    def get_or_create(self, username: str) -> WorkspaceRecord:
        """Return the user's workspace, and create it when it does not exist.

        Args:
            username: The owner; it must not be empty.

        Returns:
            The user's one workspace.

        Raises:
            ValueError: The username is empty.
        """
        if not username:
            raise ValueError("a workspace needs a username")
        workspace_id = workspace_id_for(username)
        insert = postgres_insert if self._engine.dialect.name == "postgresql" else sqlite_insert
        now = datetime.now(UTC)
        with Session(self._engine) as session:
            # A new workspace has no data in an old format, so it starts at the current version.
            session.execute(
                insert(WorkspaceModel)
                .values(
                    id=workspace_id,
                    username=username,
                    policy={},
                    storage_version=current_version(),
                    created_at=now,
                    updated_at=now,
                )
                .on_conflict_do_nothing(index_elements=["id"])
            )
            session.commit()
        return self.get(workspace_id)

    def set_budget(self, workspace_id: str, budget_id: str | None) -> WorkspaceRecord:
        """Set the execution budget that the workspace's runs spend from.

        Args:
            workspace_id: The workspace.
            budget_id: The budget, or ``None`` to clear it.

        Returns:
            The workspace after the change.

        Raises:
            LookupError: The workspace does not exist.
        """
        with Session(self._engine) as session:
            result = session.execute(
                update(WorkspaceModel)
                .where(WorkspaceModel.id == workspace_id)
                .values(execution_budget_id=budget_id, updated_at=datetime.now(UTC))
            )
            if result.rowcount == 0:
                raise LookupError(f"workspace {workspace_id} does not exist")
            session.commit()
        return self.get(workspace_id)

    def budget_admission(self, budget_id: str, username: str, job_id: str) -> BudgetAdmission:
        """Read a budget's state and generation, and a job's generation, with one plain select that locks no row.

        ``BudgetService.get`` locks the wallet and budget rows. An agent reads
        these before each paid step, so the read must stay off the hot rows.

        Args:
            budget_id: The budget.
            username: The budget's owner.
            job_id: The job whose ``execution_generation`` to read.

        Returns:
            The budget's state and generation, and the job's ``execution_generation`` (``None`` when the job does
            not exist).

        Raises:
            LookupError: The user has no budget with this id.
        """
        job_generation = (
            select(JobModel.execution_generation).where(JobModel.optimization_id == job_id).scalar_subquery()
        )
        with Session(self._engine) as session:
            row = session.execute(
                select(ExecutionBudgetModel.state, ExecutionBudgetModel.generation, job_generation).where(
                    ExecutionBudgetModel.id == budget_id, ExecutionBudgetModel.username == username
                )
            ).first()
        if row is None:
            raise LookupError(f"budget {budget_id} does not exist")
        return BudgetAdmission(state=row[0], generation=row[1], execution_generation=row[2])
