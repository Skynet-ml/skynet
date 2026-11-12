"""Telemetry models emitted while an optimization runs (progress events + log lines)."""

from __future__ import annotations

from datetime import datetime
from typing import Any

from pydantic import BaseModel, Field


class ProgressEvent(BaseModel):
    """Structured telemetry emitted while an optimization runs."""

    timestamp: datetime
    event: str | None = None
    metrics: dict[str, Any] = Field(default_factory=dict)


class JobLogEntry(BaseModel):
    """Log line captured from DSPy/optimizer loggers."""

    timestamp: datetime
    level: str
    logger: str
    message: str
    # Structured run-log fields (core.run_log). ``id`` is the live stream's
    # resume cursor; rows from before these existed are the host's.
    id: int | None = None
    source: str = "host"
    event: str | None = None
    fields: dict[str, Any] | None = None
    candidate: str | None = None
    case: str | None = None
