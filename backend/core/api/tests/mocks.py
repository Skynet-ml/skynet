"""Centralized mock/fake builders for ``core.api.tests``.

Domain fakes that were previously scattered inline across individual test
files live here. Test-specific values (e.g. a particular metric pair that
the test is asserting against) stay inline in the test — only the generic
"give me any valid fake X" belongs here.

Example:
    >>> from core.api.tests.mocks import (
    ...     fake_background_worker,
    ... )
"""

from __future__ import annotations

from datetime import UTC, datetime
from types import SimpleNamespace
from typing import Any
from unittest.mock import MagicMock
from uuid import uuid4

from sqlalchemy import create_engine, delete
from sqlalchemy.engine import Engine
from sqlalchemy.orm import Session
from sqlalchemy.pool import StaticPool

from ...storage.models import Base, RunCallModel, RunCandidateModel
from ...storage.run_call_store import PostgresRunCallStore


class _BaseFakeJobStore:
    """In-memory stand-in for the production job store.

    Implements only the methods that the routers under test reach for, with no
    persistence and no concurrency guarantees.
    """

    def __init__(self) -> None:
        """Initialise empty job, log, progress, and staged-dataset dictionaries."""
        self._jobs: dict[str, dict] = {}
        self._logs: dict[str, list] = {}
        self._progress: dict[str, list] = {}
        self._staged: dict[str, dict] = {}
        self._checkpoints: dict[str, dict[int, dict]] = {}
        self._agent_runs: dict[str, dict[int, dict]] = {}
        self._run_call_engine: Engine | None = None

    def _run_call_store(self) -> PostgresRunCallStore:
        """Return the run record store on a private in-memory SQLite engine.

        The fake reuses the real store so router tests see the same secret
        strip, cut and cursor rules as production. The engine is made on first
        use, so tests that never touch the run record pay nothing.

        Returns:
            The store bound to this fake's engine.
        """
        if self._run_call_engine is None:
            self._run_call_engine = create_engine(
                "sqlite:///:memory:", connect_args={"check_same_thread": False}, poolclass=StaticPool
            )
            Base.metadata.create_all(
                self._run_call_engine, tables=[RunCallModel.__table__, RunCandidateModel.__table__]
            )
        return PostgresRunCallStore(self._run_call_engine)

    def stage_dataset(self, username: str, dataset_filename: str, rows: list[dict]) -> str:
        """Persist staged rows in memory and return their opaque id.

        Args:
            username: Owner of the staged copy.
            dataset_filename: Original filename (kept for parity with prod).
            rows: Non-empty parsed rows.

        Returns:
            The staged-dataset id.

        Raises:
            ValueError: When ``rows`` is empty.
        """
        if not rows:
            raise ValueError("staged dataset rows must be non-empty")
        staged_id = uuid4().hex
        self._staged[staged_id] = {
            "username": username,
            "dataset_filename": dataset_filename,
            "rows": list(rows),
        }
        return staged_id

    def get_staged_dataset(self, staged_dataset_id: str, username: str) -> list[dict] | None:
        """Return staged rows scoped to ``username``, or None when unmatched.

        Args:
            staged_dataset_id: Id from :meth:`stage_dataset`.
            username: Authenticated caller; guards cross-user reads.

        Returns:
            The persisted rows, or None when missing or owned by another user.
        """
        entry = self._staged.get(staged_dataset_id)
        if entry is None or entry["username"] != username:
            return None
        return list(entry["rows"])

    def seed_job(self, optimization_id: str, **fields: Any) -> dict:
        """Insert a job row, with sensible defaults for any fields not given.

        Args:
            optimization_id: Identifier for the synthesised job.
            **fields: Overrides applied on top of the default success row.

        Returns:
            The stored job row.
        """
        now = datetime.now(UTC).isoformat()
        job = {
            "optimization_id": optimization_id,
            "status": "success",
            "created_at": now,
            "started_at": now,
            "completed_at": now,
            "payload_overview": {},
            "payload": {},
            "result": None,
            "latest_metrics": {},
            "message": None,
            **fields,
        }
        self._jobs[optimization_id] = job
        return job

    def update_job(self, optimization_id: str, **fields: Any) -> None:
        """Apply field overrides to an existing job.

        Args:
            optimization_id: Identifier of the job to mutate.
            **fields: Fields to merge into the stored row.
        """
        self._jobs[optimization_id].update(fields)

    def delete_job(self, optimization_id: str) -> None:
        """Remove a job and its associated logs and progress events.

        Args:
            optimization_id: Identifier of the job to delete.
        """
        self._jobs.pop(optimization_id, None)
        self._logs.pop(optimization_id, None)
        self._progress.pop(optimization_id, None)
        self._checkpoints.pop(optimization_id, None)
        self._agent_runs.pop(optimization_id, None)
        if self._run_call_engine is not None:
            with Session(self._run_call_engine) as session:
                session.execute(delete(RunCallModel).where(RunCallModel.optimization_id == optimization_id))
                session.execute(delete(RunCandidateModel).where(RunCandidateModel.optimization_id == optimization_id))
                session.commit()

    def list_run_calls(
        self, optimization_id: str, candidate_id: str, *, since: int = 0, limit: int = 100
    ) -> list[dict]:
        """Return a candidate's calls after ``since`` through the real store."""
        return self._run_call_store().list_calls(optimization_id, candidate_id, since=since, limit=limit)

    def get_run_candidate(self, optimization_id: str, candidate_id: str) -> dict | None:
        """Return one candidate's summary through the real store, or ``None``."""
        return self._run_call_store().get_candidate(optimization_id, candidate_id)

    def list_run_candidates(self, optimization_id: str, *, limit: int = 100, offset: int = 0) -> list[dict]:
        """Return one page of the run's candidate summaries through the real store."""
        return self._run_call_store().list_candidates(optimization_id, limit=limit, offset=offset)

    def save_agent_run(self, optimization_id: str, run: dict) -> None:
        """Store one agent run record under its ``run_id``.

        Args:
            optimization_id: Owning job id.
            run: The record, keyed by ``run_id``.
        """
        self._agent_runs.setdefault(optimization_id, {})[int(run["run_id"])] = dict(run)

    def append_agent_run_transcript(self, optimization_id: str, run_id: int, text: str) -> None:
        """Extend a stored run's transcript, ignoring unknown runs.

        Args:
            optimization_id: Owning job id.
            run_id: The run's ordinal.
            text: The transcript piece to add.
        """
        run = self._agent_runs.get(optimization_id, {}).get(run_id)
        if run is not None:
            run["transcript"] = run.get("transcript", "") + text

    def get_agent_run(self, optimization_id: str, run_id: int) -> dict | None:
        """Return a stored run record, or ``None``.

        Args:
            optimization_id: Owning job id.
            run_id: The run's ordinal.

        Returns:
            A copy of the record, or ``None`` when unknown.
        """
        run = self._agent_runs.get(optimization_id, {}).get(run_id)
        return None if run is None else dict(run)

    def save_gepa_checkpoint(self, optimization_id: str, data: bytes, iteration: int, pair_index: int = -1) -> None:
        """Store GEPA checkpoint bytes in memory (test seam for resume tests)."""
        self._checkpoints.setdefault(optimization_id, {})[pair_index] = {"data": data, "iteration": iteration}

    def get_gepa_checkpoint(self, optimization_id: str, pair_index: int = -1):
        """Return one run/pair's in-memory checkpoint as a record, or ``None``."""
        row = self._checkpoints.get(optimization_id, {}).get(pair_index)
        if row is None:
            return None
        return SimpleNamespace(
            optimization_id=optimization_id,
            pair_index=pair_index,
            data=row["data"],
            iteration=row["iteration"],
            stored_bytes=len(row["data"]),
        )

    def delete_gepa_checkpoint(self, optimization_id: str, pair_index: int = -1) -> None:
        """Drop one run/pair's in-memory checkpoint."""
        self._checkpoints.get(optimization_id, {}).pop(pair_index, None)

    def has_gepa_checkpoint(self, optimization_id: str) -> bool:
        """Return whether any checkpoint exists for the job (single run or any pair)."""
        return bool(self._checkpoints.get(optimization_id))

    def requeue_for_resume(self, optimization_id: str, *, bump_attempts: bool = True):
        """Flip a job back to ``pending`` in place, optionally bumping its attempt count."""
        job = self._jobs.get(optimization_id)
        if job is None:
            return None
        current = int(job.get("attempts") or 0)
        next_attempt = current + 1 if bump_attempts else current
        job["attempts"] = next_attempt
        job["status"] = "pending"
        start_raw = job.get("started_at")
        if start_raw:
            start = datetime.fromisoformat(start_raw)
            end = datetime.fromisoformat(job.get("completed_at") or datetime.now(UTC).isoformat())
            start = start if start.tzinfo else start.replace(tzinfo=UTC)
            end = end if end.tzinfo else end.replace(tzinfo=UTC)
            job["accumulated_runtime_seconds"] = float(job.get("accumulated_runtime_seconds") or 0.0) + max(
                0.0, (end - start).total_seconds()
            )
        job["completed_at"] = None
        job["started_at"] = None
        job["message"] = "Resuming"
        return next_attempt

    def requeue_for_rerun(self, optimization_id: str) -> bool:
        """Reset a job in place for a fresh re-run, clearing its prior artefacts."""
        job = self._jobs.get(optimization_id)
        if job is None:
            return False
        self._logs.pop(optimization_id, None)
        self._progress.pop(optimization_id, None)
        self._checkpoints.pop(optimization_id, None)
        job["status"] = "pending"
        job["started_at"] = None
        job["completed_at"] = None
        job["message"] = None
        job["latest_metrics"] = {}
        job["result"] = None
        job["attempts"] = 0
        job["accumulated_runtime_seconds"] = 0.0
        return True

    def get_job(self, optimization_id: str) -> dict:
        """Return a copy of the stored job row.

        Args:
            optimization_id: Identifier of the job to fetch.

        Returns:
            A shallow copy of the job row.

        Raises:
            KeyError: If no job with the given identifier exists.
        """
        if optimization_id not in self._jobs:
            raise KeyError(optimization_id)
        return dict(self._jobs[optimization_id])

    def job_exists(self, optimization_id: str) -> bool:
        """Report whether a job with the given identifier is stored.

        Args:
            optimization_id: Identifier to look up.

        Returns:
            ``True`` if the job is present, ``False`` otherwise.
        """
        return optimization_id in self._jobs

    def list_jobs(self, **kwargs: Any) -> list[dict]:
        """Return jobs, optionally filtered and paginated.

        Args:
            **kwargs: Optional ``status``, ``username``, ``optimization_type``,
                ``limit``, and ``offset`` filters.

        Returns:
            The matching jobs as a list of rows.
        """
        rows = list(self._jobs.values())
        status = kwargs.get("status")
        username = kwargs.get("username")
        optimization_type = kwargs.get("optimization_type")
        if status:
            rows = [r for r in rows if r.get("status") == status]
        if username:
            rows = [r for r in rows if r.get("payload_overview", {}).get("username") == username]
        if optimization_type:
            rows = [r for r in rows if r.get("payload_overview", {}).get("job_type") == optimization_type]
        limit = kwargs.get("limit", len(rows))
        offset = kwargs.get("offset", 0)
        return rows[offset : offset + limit]

    def count_jobs(self, **kwargs: Any) -> int:
        """Count jobs matching the supplied filters.

        Args:
            **kwargs: Same filters accepted by ``list_jobs``; pagination
                parameters are ignored.

        Returns:
            The number of matching jobs.
        """
        k = dict(kwargs)
        k.pop("limit", None)
        k.pop("offset", None)
        return len(self.list_jobs(limit=10**9, offset=0, **k))

    def get_logs(self, optimization_id: str, **kwargs: Any) -> list:
        """Return log rows for a job, optionally filtered and paginated.

        Args:
            optimization_id: Identifier of the job whose logs are returned.
            **kwargs: Optional ``level``, ``limit``, and ``offset`` filters.

        Returns:
            The matching log rows.
        """
        rows = list(self._logs.get(optimization_id, []))
        level = kwargs.get("level")
        if level:
            rows = [r for r in rows if r.get("level") == level]
        limit = kwargs.get("limit")
        offset = kwargs.get("offset", 0)
        rows = rows[offset:]
        if limit is not None:
            rows = rows[:limit]
        return rows

    def get_log_count(self, optimization_id: str) -> int:
        """Return the number of stored log rows for a job.

        Args:
            optimization_id: Identifier of the job to inspect.

        Returns:
            The log count, ``0`` if the job has no logs.
        """
        return len(self._logs.get(optimization_id, []))

    def get_progress_events(self, optimization_id: str, *, since: int = 0) -> list:
        """Return the stored progress events for a job, skipping ``since`` leading rows.

        Args:
            optimization_id: Identifier of the job to inspect.
            since: Number of leading events to skip for delta fetches.

        Returns:
            The progress event list from offset ``since`` onward.
        """
        return list(self._progress.get(optimization_id, []))[since:]

    def get_progress_count(self, optimization_id: str) -> int:
        """Return the number of stored progress events for a job.

        Args:
            optimization_id: Identifier of the job to inspect.

        Returns:
            The progress event count, ``0`` if the job has no events.
        """
        return len(self._progress.get(optimization_id, []))

    def set_payload_overview(self, optimization_id: str, overview: dict) -> None:
        """Replace the payload overview stored on a job.

        Args:
            optimization_id: Identifier of the job to update.
            overview: New overview to store.
        """
        self._jobs[optimization_id]["payload_overview"] = dict(overview)

    def seed_raw(
        self,
        optimization_id: str,
        *,
        job: dict,
        logs: list | None = None,
        progress: list | None = None,
    ) -> None:
        """Seed an entire job row, plus optional logs and progress events.

        Args:
            optimization_id: Identifier of the synthesised job.
            job: The full job row to store.
            logs: Optional log rows to associate with the job.
            progress: Optional progress events to associate with the job.
        """
        self._jobs[optimization_id] = job
        self._logs[optimization_id] = list(logs or [])
        self._progress[optimization_id] = list(progress or [])

    def delete_jobs(self, optimization_ids: list[str]) -> None:
        """Delete a batch of jobs by identifier.

        Args:
            optimization_ids: Identifiers of the jobs to delete.
        """
        for oid in optimization_ids:
            self.delete_job(oid)

    def get_jobs_status_by_ids(self, optimization_ids: list[str]) -> dict[str, str | None]:
        """Return the status of each requested job.

        Args:
            optimization_ids: Identifiers to look up.

        Returns:
            A mapping from identifier to status; missing jobs map to ``None``.
        """
        return {oid: self._jobs[oid]["status"] if oid in self._jobs else None for oid in optimization_ids}


class FakeJobStore(_BaseFakeJobStore):
    """Concrete in-memory job store used by router unit tests."""


def fake_background_worker() -> MagicMock:
    """Build a ``MagicMock`` mimicking a healthy idle background worker.

    Returns:
        A pre-configured ``MagicMock`` exposing the methods the API expects.
    """
    w = MagicMock()
    w.threads_alive.return_value = True
    w.seconds_since_last_activity.return_value = 0.0
    w.queue_size.return_value = 0
    w.active_jobs.return_value = 0
    w.thread_count.return_value = 2
    w.submit_job = MagicMock()
    w.enqueue_job = MagicMock()
    w.cancel = MagicMock()
    w.pending_ids = []
    w.status.return_value = "idle"
    return w
