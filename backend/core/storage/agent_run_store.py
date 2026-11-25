"""Persistence seam for the sandboxed agent runs of black-box jobs.

Only stored text or agent runs have these rows: no run writes them since every
run is a repository run (SKY-834). This store reads the rows in
``blackbox_agent_runs``, keyed by ``(optimization_id, run_id)``, for the run view.
"""

from __future__ import annotations

from datetime import UTC, datetime
from typing import Any

from sqlalchemy.engine import Engine
from sqlalchemy.orm import Session

from .models import BlackboxAgentRunModel


def _iso(value: datetime | None) -> str | None:
    """Return a stored timestamp as an aware ISO string; SQLite hands back naive UTC."""
    if value is None:
        return None
    if value.tzinfo is None:
        value = value.replace(tzinfo=UTC)
    return value.isoformat()


class PostgresAgentRunStore:
    """Agent run records in ``blackbox_agent_runs``, keyed by ``(optimization_id, run_id)``."""

    def __init__(self, engine: Engine) -> None:
        """Bind the store to a SQLAlchemy engine.

        Args:
            engine: Engine whose schema carries ``blackbox_agent_runs``.
        """
        self._engine = engine

    def get(self, optimization_id: str, run_id: int) -> dict[str, Any] | None:
        """Return one run's record, or ``None``.

        Args:
            optimization_id: Owning job id.
            run_id: The run's ordinal within the job.

        Returns:
            The record with ISO timestamps, or ``None`` when no row exists.
        """
        with Session(self._engine) as session:
            row = session.get(BlackboxAgentRunModel, (optimization_id, run_id))
            return None if row is None else self._to_record(row)

    @staticmethod
    def _to_record(row: BlackboxAgentRunModel) -> dict[str, Any]:
        """Project an ORM row onto the record shape the recorder sends."""
        return {
            "run_id": row.run_id,
            "phase": row.phase,
            "trial": row.trial,
            "example_id": row.example_id,
            "case_id": row.case_id,
            "label": row.label,
            "status": row.status,
            "started_at": _iso(row.started_at),
            "finished_at": _iso(row.finished_at),
            "model": row.model,
            "exit_code": row.exit_code,
            "timed_out": row.timed_out,
            "elapsed_seconds": row.elapsed_seconds,
            "error": row.error,
            "usage": dict(row.usage or {}),
            "check": row.check_result,
            "output": row.output,
            "transcript": row.transcript or "",
        }
