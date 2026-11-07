"""Response models of the run record read routes (SKY-364).

``GET /optimizations/{id}/candidates`` pages through a run's candidates with
an ``offset`` and ``GET /optimizations/{id}/candidates/{candidate_id}/calls``
pages through one candidate's relay calls with a ``since`` cursor. The classes carry no
docstrings on purpose: a ``BaseModel`` docstring becomes part of the OpenAPI
schema.
"""

from __future__ import annotations

from typing import Literal

from pydantic import BaseModel, Field

RunCallKind = Literal["model", "mcp", "stage", "evaluator", "host"]
RunCandidateState = Literal["running", "done", "failed"]


# One relay call. ``seq`` counts the candidate's calls from 1 and is the
# ``since`` cursor. ``request``/``response`` are cut at the store's cap when
# ``*_truncated`` is set; ``*_bytes`` is always the full size. Secret values
# are already replaced with ``[REDACTED]``.
class RunCallRecord(BaseModel):
    id: int
    seq: int
    candidate_id: str
    role: str = ""
    kind: RunCallKind
    target: str = ""
    started_at: str | None = None
    duration_ms: int | None = None
    status: str
    failure_reason: str | None = None
    cost_usd: float | None = None
    request: str = ""
    request_bytes: int = 0
    request_truncated: bool = False
    response: str = ""
    response_bytes: int = 0
    response_truncated: bool = False


# One page of a candidate's calls after ``since``. Poll again with
# ``next_since`` while ``running`` is true or ``has_more`` is true.
class RunCallPage(BaseModel):
    optimization_id: str
    candidate_id: str
    running: bool
    calls: list[RunCallRecord] = Field(default_factory=list)
    next_since: int
    has_more: bool = False


# One candidate of a run with its call count and cost so far.
class RunCandidateSummary(BaseModel):
    candidate_id: str
    role: str = ""
    state: RunCandidateState
    call_count: int = 0
    cost_usd: float = 0.0
    started_at: str | None = None
    updated_at: str | None = None


# One page of a run's candidates in start order. Ask again with
# ``offset=next_offset`` while ``has_more`` is true.
class RunCandidateList(BaseModel):
    optimization_id: str
    candidates: list[RunCandidateSummary] = Field(default_factory=list)
    next_offset: int = 0
    has_more: bool = False
