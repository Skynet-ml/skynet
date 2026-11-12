"""Persistence seam for resuming GEPA optimizations from their checkpoints.

The GEPA engine writes its full state (``gepa_state.bin``) at every iteration:
the candidate population, per-candidate validation scores, the Pareto front, the
iteration counter and the consumed metric-call budget. Persisting the latest
copy lets a run that died mid-optimization resume from its last completed
iteration while charging every physical request, including upstream's resumed seed evaluation.

The seam is keyed by ``(optimization_id, pair_index)``: a run uses the sentinel
``pair_index = -1``. The bytes go through this store — mirroring the
dataset-library blob seam — so they can later move behind an object store without
touching the worker, the resume endpoint, or the storage meter.
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import UTC, datetime
from typing import Any

from sqlalchemy import delete, select
from sqlalchemy.engine import Engine
from sqlalchemy.orm import Session

from .models import GepaCheckpointModel, JobModel

SINGLE_RUN_PAIR_INDEX = -1


@dataclass(frozen=True)
class GepaCheckpoint:
    """One run's saved GEPA state plus the iteration it was taken at."""

    optimization_id: str
    pair_index: int
    iteration: int
    data: bytes
    stored_bytes: int
    manifest: dict[str, Any] | None = None


class PostgresCheckpointBlobStore:
    """GEPA state blobs in ``gepa_checkpoints``, keyed by ``(optimization_id, pair_index)``."""

    def __init__(self, engine: Engine) -> None:
        """Bind the checkpoint store to a SQLAlchemy engine.

        Args:
            engine: Engine whose schema carries ``gepa_checkpoints``.
        """
        self._engine = engine

    def put(
        self,
        optimization_id: str,
        *,
        data: bytes,
        iteration: int,
        pair_index: int = SINGLE_RUN_PAIR_INDEX,
        manifest: dict[str, Any] | None = None,
        expected_generation: int | None = None,
    ) -> bool:
        """Insert or replace the GEPA state bytes for one run.

        Args:
            optimization_id: Owning job id.
            data: The raw ``gepa_state.bin`` bytes for the latest iteration.
            iteration: The iteration index the state was saved at.
            pair_index: Checkpoint key; ``-1`` for a run.
            manifest: Exact source, task, runtime, and integrity evidence for recovery.
            expected_generation: Worker epoch allowed to replace this checkpoint.

        Returns:
            Whether the current generation stored the checkpoint.
        """
        now = datetime.now(UTC)
        with Session(self._engine) as session:
            if (
                expected_generation is not None
                and session.scalar(
                    select(JobModel)
                    .where(
                        JobModel.optimization_id == optimization_id,
                        JobModel.execution_generation == expected_generation,
                    )
                    .with_for_update()
                )
                is None
            ):
                return False
            existing = session.get(GepaCheckpointModel, (optimization_id, pair_index))
            if existing is None:
                session.add(
                    GepaCheckpointModel(
                        optimization_id=optimization_id,
                        pair_index=pair_index,
                        iteration=iteration,
                        data=data,
                        manifest=manifest,
                        stored_bytes=len(data),
                        updated_at=now,
                    )
                )
            else:
                existing.iteration = iteration
                existing.data = data
                existing.manifest = manifest
                existing.stored_bytes = len(data)
                existing.updated_at = now
            session.commit()
            return True

    def get(self, optimization_id: str, pair_index: int = SINGLE_RUN_PAIR_INDEX) -> GepaCheckpoint | None:
        """Return the saved checkpoint for one run, or ``None``.

        Args:
            optimization_id: Job whose checkpoint is read.
            pair_index: Checkpoint key; ``-1`` for a run.

        Returns:
            The :class:`GepaCheckpoint`, or ``None`` when no row exists.
        """
        with Session(self._engine) as session:
            row = session.get(GepaCheckpointModel, (optimization_id, pair_index))
            return self._to_checkpoint(row) if row is not None else None

    def list_for_optimization(self, optimization_id: str) -> list[GepaCheckpoint]:
        """Return every saved checkpoint for a job.

        Args:
            optimization_id: Job whose checkpoints are read.

        Returns:
            The job's :class:`GepaCheckpoint` rows (possibly empty).
        """
        with Session(self._engine) as session:
            rows = session.scalars(
                select(GepaCheckpointModel).where(GepaCheckpointModel.optimization_id == optimization_id)
            ).all()
            return [self._to_checkpoint(row) for row in rows]

    def delete(self, optimization_id: str, pair_index: int = SINGLE_RUN_PAIR_INDEX) -> None:
        """Remove one run's checkpoint if present.

        Args:
            optimization_id: Owning job id.
            pair_index: Checkpoint key; ``-1`` for a run.
        """
        with Session(self._engine) as session:
            session.execute(
                delete(GepaCheckpointModel).where(
                    GepaCheckpointModel.optimization_id == optimization_id,
                    GepaCheckpointModel.pair_index == pair_index,
                )
            )
            session.commit()

    def delete_all(self, optimization_id: str) -> None:
        """Remove every checkpoint for a job.

        Args:
            optimization_id: Job whose checkpoints are dropped.
        """
        with Session(self._engine) as session:
            session.execute(delete(GepaCheckpointModel).where(GepaCheckpointModel.optimization_id == optimization_id))
            session.commit()

    def has_any(self, optimization_id: str) -> bool:
        """Return whether the job has any saved checkpoint.

        Args:
            optimization_id: Job to test.

        Returns:
            ``True`` when at least one checkpoint row exists.
        """
        with Session(self._engine) as session:
            row = (
                session.query(GepaCheckpointModel.pair_index)
                .filter(GepaCheckpointModel.optimization_id == optimization_id)
                .first()
            )
            return row is not None

    @staticmethod
    def _to_checkpoint(row: GepaCheckpointModel) -> GepaCheckpoint:
        """Project an ORM row onto an immutable :class:`GepaCheckpoint`."""
        return GepaCheckpoint(
            optimization_id=row.optimization_id,
            pair_index=row.pair_index,
            iteration=row.iteration,
            data=row.data,
            stored_bytes=row.stored_bytes,
            manifest=row.manifest,
        )
