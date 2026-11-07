"""Metric-hardening job shapes: the API contract and the table that keeps each job.

A job is kept as one JSON document next to the columns that the API filters
on, so the contract can grow without a migration for each field.
"""

from __future__ import annotations

import uuid
from datetime import UTC, datetime
from typing import Any, Literal

from pydantic import BaseModel, Field
from sqlalchemy import DateTime, Index, String, text
from sqlalchemy.orm import Mapped, mapped_column

from ..constants import HARDENING_DEFAULT_ROUND_CAP_USD, HARDENING_DEFAULT_ROUNDS
from ..storage.models import JSON_STORE, Base

HardeningStatus = Literal[
    "preflight",
    "preflight_failed",
    "running",
    "waiting_review",
    "waiting_fix",
    "clean_stop",
    "budget_stop",
    "done",
    "rejected",
    "failed",
    "stale",
]
HardeningView = Literal["black_box", "white_box"]
HardeningPlace = Literal["local", "cloud"]
CheckResult = Literal["pass", "fail", "pending"]
RoundStatus = Literal["running", "waiting_review", "waiting_fix", "clean", "approved", "fixed", "rejected", "stopped"]

ACTIVE_STATUSES: frozenset[str] = frozenset({"preflight", "running", "waiting_review", "waiting_fix"})
_ACTIVE_WHERE = text("status IN ('preflight', 'running', 'waiting_review', 'waiting_fix')")


# One claim from the attacker, with the judge's label and the user's verdict.
# A "case" is one concrete input; a "method" is a general trick (D47).
class HardeningExploit(BaseModel):
    id: str
    kind: Literal["case", "method"]
    label: Literal["exploit", "valid"]
    confidence: float = Field(ge=0.0, le=1.0)
    reason: str
    unsure: bool
    patch_diff: str
    why_md: str
    user_verdict: Literal["confirmed", "rejected"] | None = None


# reference_not_lower stays null when the metric has no reference solution.
class HardeningFixChecks(BaseModel):
    suite_replay: CheckResult = "pending"
    base_score_unchanged: CheckResult = "pending"
    reference_not_lower: CheckResult | None = None


# The metric fix the agent drafts for a round's confirmed exploits.
class HardeningFix(BaseModel):
    diff: str
    checks: HardeningFixChecks = Field(default_factory=HardeningFixChecks)
    user_decision: Literal["approved", "rejected"] | None = None


# One attack round with its exploits and, once drafted, its fix.
class HardeningRound(BaseModel):
    index: int = Field(ge=1)
    status: RoundStatus
    exploits: list[HardeningExploit] = Field(default_factory=list)
    fix: HardeningFix | None = None


# The contract the hardening page polls (SKY-357). Keep fields in step with it.
class HardeningJob(BaseModel):
    id: str
    status: HardeningStatus
    round: int = Field(ge=0)
    rounds: int = Field(ge=1)
    round_cap_usd: float = Field(gt=0)
    reserved_usd: float = Field(ge=0)
    spent_usd: float = Field(ge=0)
    place: HardeningPlace
    attacker_model: str
    judge_model: str
    view: HardeningView
    preflight_failures: list[str] = Field(default_factory=list)
    rounds_detail: list[HardeningRound] = Field(default_factory=list)
    suite_count: int = Field(default=0, ge=0)
    fixed_count: int = Field(default=0, ge=0)
    error: str | None = None


# Body of POST /hardening/jobs.
class CreateHardeningJobRequest(BaseModel):
    draft_id: str = Field(min_length=1, max_length=255)
    rounds: int = Field(default=HARDENING_DEFAULT_ROUNDS, ge=1, le=20)
    round_cap_usd: float = Field(default=HARDENING_DEFAULT_ROUND_CAP_USD, gt=0, le=100)
    attacker_model: str = Field(min_length=1, max_length=255)
    judge_model: str = Field(min_length=1, max_length=255)
    view: HardeningView = "black_box"


# Body of POST /hardening/jobs/{job_id}/exploits/{exploit_id}/verdict.
class HardeningVerdictRequest(BaseModel):
    confirmed: bool


# Body of POST /hardening/jobs/{job_id}/fix.
class HardeningFixDecisionRequest(BaseModel):
    approve: bool


class HardeningJobModel(Base):
    """One metric-hardening job of one user, for one setup draft."""

    __tablename__ = "hardening_jobs"
    __table_args__ = (
        Index("ix_hardening_jobs_owner_draft_created", "username", "draft_id", "created_at"),
        Index(
            "uq_hardening_jobs_active_draft",
            "username",
            "draft_id",
            unique=True,
            postgresql_where=_ACTIVE_WHERE,
            sqlite_where=_ACTIVE_WHERE,
        ),
    )

    id: Mapped[str] = mapped_column(String(36), primary_key=True, default=lambda: str(uuid.uuid4()))
    username: Mapped[str] = mapped_column(String(255), nullable=False, index=True)
    draft_id: Mapped[str] = mapped_column(String(255), nullable=False)
    status: Mapped[str] = mapped_column(String(32), nullable=False, index=True)
    job: Mapped[dict[str, Any]] = mapped_column(JSON_STORE, nullable=False)
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), nullable=False, default=lambda: datetime.now(UTC)
    )
    updated_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), nullable=False, default=lambda: datetime.now(UTC)
    )
