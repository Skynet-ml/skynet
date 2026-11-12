"""Log handler that routes a job's sandbox output records into the job store."""

from __future__ import annotations

import contextlib
import logging
import os
from collections.abc import Iterator
from datetime import UTC, datetime

from .. import run_log
from ..service_gateway.optimization.blackbox import sandbox_log
from ..storage import JobStore


class SandboxLogRouter(logging.Handler):
    """Persist one job's sandbox output records, whichever thread produced them.

    Sandboxes a protected run opens on the worker's own broker and scorer
    threads would otherwise never reach the run log: those threads are not the
    job's, and nothing in the worker process forwards their records.
    """

    def __init__(self, optimization_id: str, jobs: JobStore) -> None:
        """Bind the router to one job in the current process.

        Args:
            optimization_id: Job whose sandboxes' records this router persists.
            jobs: Storage backend used to persist log entries.
        """
        super().__init__(logging.DEBUG)
        self._optimization_id = optimization_id
        self._jobs = jobs
        # A forked optimization child inherits this handler; it forwards its own
        # records through log events, so persisting them here would double them.
        self._pid = os.getpid()

    def emit(self, record: logging.LogRecord) -> None:
        """Persist ``record`` when it comes from this job's sandbox in this process.

        Args:
            record: A sandbox output record.
        """
        if os.getpid() != self._pid or getattr(record, "sandbox_owner", None) != self._optimization_id:
            return
        try:
            self._jobs.append_log(
                self._optimization_id,
                level=record.levelname,
                logger_name=record.name,
                message=record.getMessage(),
                timestamp=datetime.fromtimestamp(record.created, tz=UTC),
                **run_log.entry_fields(record),
            )
        except Exception:
            self.handleError(record)


@contextlib.contextmanager
def route_sandbox_logs(optimization_id: str, jobs: JobStore) -> Iterator[None]:
    """Persist the job's sandbox output records into its run log while the block runs.

    Args:
        optimization_id: Job being processed.
        jobs: Storage backend used to persist log entries.

    Yields:
        Nothing; the router is detached on exit.
    """
    router = SandboxLogRouter(optimization_id, jobs)
    targets = (sandbox_log.logger, sandbox_log.stream_logger)
    for target in targets:
        target.addHandler(router)
    try:
        yield
    finally:
        for target in targets:
            target.removeHandler(router)
