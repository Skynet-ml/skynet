"""Worker test fixtures and the in-memory FakeJobStore used by the tests."""

from __future__ import annotations

from collections.abc import Iterator
from datetime import UTC, datetime
from typing import Any
from unittest.mock import patch

import pytest


@pytest.fixture(autouse=True)
def _stop_started_patches() -> Iterator[None]:
    """Stop the patches a helper started, so one test's fakes never reach the next."""
    yield
    patch.stopall()


class FakeJobStore:
    """Worker-tier subset of the JobStore protocol backed by in-memory dicts.

    Implements only the methods the worker actually calls; the full protocol
    impl lives in ``core/storage/tests/conftest.py``.
    """

    def __init__(self) -> None:
        """Initialise empty in-memory storage and call-recording buffers."""
        self._jobs: dict[str, dict[str, Any]] = {}
        self._logs: dict[str, list[dict[str, Any]]] = {}
        self._progress: dict[str, list[tuple[str | None, dict[str, Any]]]] = {}
        self._notified: set[str] = set()
        self.append_log_calls: list[dict[str, Any]] = []
        self.record_progress_calls: list[tuple[str, str | None, dict[str, Any]]] = []
        self.agent_run_calls: list[tuple[str, str, Any]] = []
        self.requeue_calls: list[dict[str, Any]] = []

    def claim_completion_notification(self, optimization_id: str) -> bool:
        """Atomically claim the notification right for ``optimization_id``.

        Mirrors the real ``RemoteDBJobStore.claim_completion_notification``:
        the first caller for each id returns ``True``; later callers (or
        callers for unseeded ids) return ``False``.

        Args:
            optimization_id: ID of the job whose notification is being claimed.

        Returns:
            ``True`` exactly once per id; ``False`` thereafter or when missing.
        """
        if optimization_id not in self._jobs or optimization_id in self._notified:
            return False
        self._notified.add(optimization_id)
        return True

    def seed_job(self, optimization_id: str, **fields: Any) -> dict[str, Any]:
        """Create a queued job row with optional overrides and return it.

        Args:
            optimization_id: ID of the seeded job.
            **fields: Extra fields merged on top of the default row.

        Returns:
            The freshly created in-memory job record.
        """
        now = datetime.now(UTC).isoformat()
        job: dict[str, Any] = {
            "optimization_id": optimization_id,
            "status": "queued",
            "created_at": now,
            "payload": {},
            **fields,
        }
        self._jobs[optimization_id] = job
        return job

    def get_job(self, optimization_id: str) -> dict[str, Any]:
        """Return a copy of the seeded job record.

        Args:
            optimization_id: ID of the job to fetch.

        Returns:
            A shallow copy of the in-memory record.

        Raises:
            KeyError: When no job with the given ID exists.
        """
        if optimization_id not in self._jobs:
            raise KeyError(optimization_id)
        return dict(self._jobs[optimization_id])

    def update_job(self, optimization_id: str, **kwargs: Any) -> None:
        """Merge ``kwargs`` into the stored row.

        Args:
            optimization_id: ID of the job being updated.
            **kwargs: Fields to merge into the record.
        """
        self._jobs[optimization_id].update(kwargs)

    def requeue_for_resume(
        self,
        optimization_id: str,
        *,
        bump_attempts: bool = True,
        **kwargs: Any,
    ) -> int | None:
        """Re-queue a job to ``pending`` for resume, bumping the attempt count.

        Args:
            optimization_id: ID of the job to re-queue.
            bump_attempts: Whether to count this re-queue against the attempt cap.
            **kwargs: Recovery coordination fields recorded for assertions.

        Returns:
            The new attempt count, or ``None`` when the job is missing.
        """
        job = self._jobs.get(optimization_id)
        if job is None:
            return None
        self.requeue_calls.append({"optimization_id": optimization_id, "bump_attempts": bump_attempts, **kwargs})
        current = int(job.get("attempts") or 0)
        next_attempt = current + 1 if bump_attempts else current
        job.update(
            attempts=next_attempt,
            status="pending",
            claimed_by=None,
            lease_expires_at=None,
            completed_at=None,
        )
        return next_attempt

    def pin_sandbox_image(self, optimization_id: str, image: str) -> str:
        """Record the image a protected run executes in, keeping any earlier pin.

        Args:
            optimization_id: The run about to open its sandbox.
            image: The deployment's current image, used only when none is pinned.

        Returns:
            The image every sandbox of this run must boot from.

        Raises:
            KeyError: When no job with the given ID exists.
        """
        if optimization_id not in self._jobs:
            raise KeyError(optimization_id)
        job = self._jobs[optimization_id]
        if job.get("sandbox_image") is None:
            job["sandbox_image"] = image
        return str(job["sandbox_image"])

    def append_log(
        self,
        optimization_id: str,
        *,
        level: str,
        logger_name: str,
        message: str,
        timestamp: datetime | None = None,
        **run_log_fields: Any,
    ) -> None:
        """Persist a log entry and record the call for assertions.

        Args:
            optimization_id: ID of the job the log belongs to.
            level: Log level name (e.g. ``"INFO"``).
            logger_name: Name of the originating logger.
            message: Formatted log message.
            timestamp: When the record was emitted, or ``None``.
            **run_log_fields: ``source``, ``event``, ``fields``, ``candidate`` and ``case``.
        """
        entry = {
            "optimization_id": optimization_id,
            "level": level,
            "logger_name": logger_name,
            "message": message,
            "timestamp": timestamp,
            **run_log_fields,
        }
        self._logs.setdefault(optimization_id, []).append(entry)
        self.append_log_calls.append(entry)

    def append_logs(self, optimization_id: str, entries: list[dict[str, Any]]) -> None:
        """Persist several log entries, recording each like :meth:`append_log`.

        Args:
            optimization_id: ID of the job the logs belong to.
            entries: Entries with ``logger`` in place of ``logger_name``.
        """
        for entry in entries:
            fields = dict(entry)
            self.append_log(
                optimization_id,
                level=fields.pop("level"),
                logger_name=fields.pop("logger"),
                message=fields.pop("message"),
                **fields,
            )

    def record_progress(
        self,
        optimization_id: str,
        message: str | None,
        metrics: dict[str, Any],
    ) -> None:
        """Persist a progress event and record the call for assertions.

        Args:
            optimization_id: ID of the job the event belongs to.
            message: Phase label, or ``None``.
            metrics: Metric snapshot for this phase.
        """
        self._progress.setdefault(optimization_id, []).append((message, metrics))
        self.record_progress_calls.append((optimization_id, message, metrics))

    def save_agent_run(self, optimization_id: str, run: dict[str, Any]) -> None:
        """Record a full agent run row for assertions.

        Args:
            optimization_id: ID of the job the run belongs to.
            run: The run record.
        """
        self.agent_run_calls.append((optimization_id, "save", run))

    def append_agent_run_transcript(self, optimization_id: str, run_id: int, text: str) -> None:
        """Record a transcript delta for assertions.

        Args:
            optimization_id: ID of the job the run belongs to.
            run_id: The run's ordinal.
            text: The transcript piece.
        """
        self.agent_run_calls.append((optimization_id, "append", (run_id, text)))

    def get_logs(self, optimization_id: str, **_kwargs: Any) -> list[dict[str, Any]]:
        """Return all log entries recorded for a job.

        Args:
            optimization_id: ID of the job whose logs are requested.
            **_kwargs: Ignored; matches the JobStore protocol signature.

        Returns:
            A copy of the in-memory log list (empty if no logs).
        """
        return list(self._logs.get(optimization_id, []))


@pytest.fixture
def fake_store() -> FakeJobStore:
    """Yield a fresh FakeJobStore for a test."""
    return FakeJobStore()
