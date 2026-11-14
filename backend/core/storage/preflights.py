"""Persist reusable wizard verification evidence without storing credentials or prompts."""

from __future__ import annotations

import math
import threading
from collections.abc import Iterator
from contextlib import contextmanager
from dataclasses import dataclass, field
from datetime import UTC, datetime, timedelta
from typing import Any
from uuid import uuid4

from sqlalchemy import DateTime, ForeignKey, Integer, String, UniqueConstraint, select, text
from sqlalchemy.engine import Engine
from sqlalchemy.orm import Mapped, Session, mapped_column

from ..billing.budgets import BudgetConflictError, BudgetNotFoundError, BudgetService
from ..billing.operation_pricing import json_fingerprint
from ..config import settings
from .models import JSON_STORE, Base, BillingProviderKeyModel, ExecutionBudgetModel, ExecutionOperationModel

_COSMETIC_FIELDS = {
    "name",
    "description",
    "username",
    "is_private",
    "estimated_cents_low",
    "estimated_cents_high",
    "preflight_id",
    "preflight_fingerprint",
    "execution_budget_id",
    "execution_budget_revision",
    "execution_budget_generation",
    "max_cost_cents",
    "dataset_filename",
    "recipe",
    # The setup chat names the run's origin, not an input it executes.
    "conversation_id",
}
_EVALUATION_FIELDS = {
    "scorer",
    "seed_candidate",
    "cases",
    "split_fractions",
    "shuffle",
    "seed",
    "target",
    "dataset",
    "dataset_id",
    "staged_dataset_id",
    "column_mapping",
    "metric_code",
    "signature_code",
    "module_name",
    "module_kwargs",
    "workflow",
    "model_config",
    "token_source",
    "execution_runtime",
}


class WizardPreflightModel(Base):
    """Store the outcome of one account-owned, content-addressed setup attempt."""

    __tablename__ = "wizard_preflights"
    __table_args__ = (UniqueConstraint("budget_id", "scope", "fingerprint", name="uq_wizard_preflight_content"),)

    id: Mapped[str] = mapped_column(String(36), primary_key=True)
    budget_id: Mapped[str] = mapped_column(ForeignKey("execution_budgets.id"), nullable=False, index=True)
    username: Mapped[str] = mapped_column(String(255), nullable=False)
    scope: Mapped[str] = mapped_column(String(24), nullable=False)
    workflow: Mapped[str] = mapped_column(String(24), nullable=False)
    fingerprint: Mapped[str] = mapped_column(String(64), nullable=False)
    status: Mapped[str] = mapped_column(String(24), nullable=False)
    attempt: Mapped[int] = mapped_column(Integer, nullable=False, default=1)
    result: Mapped[dict[str, Any]] = mapped_column(JSON_STORE, nullable=False, default=dict)
    started_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False)
    finished_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    claim_token: Mapped[str | None] = mapped_column(String(36), nullable=True)
    execution_generation: Mapped[int] = mapped_column(Integer, nullable=False, default=0)
    lease_expires_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)


@dataclass(frozen=True)
class PreflightClaim:
    """Keep the private execution token separate from public setup evidence."""

    document: dict[str, Any]
    token: str | None
    generation: int
    attempt: int = 0
    # The public document hides the row's result, so a new attempt reads its ``carry`` values here.
    kept: dict[str, Any] = field(default_factory=dict)


def _utc(value: datetime) -> datetime:
    """Treat SQLite's timezone-free stored timestamps as the UTC values originally written."""
    return value.replace(tzinfo=UTC) if value.tzinfo is None else value.astimezone(UTC)


def setup_seed(budget_id: str) -> int:
    """Keep an unspecified split seed stable from setup through the attached run."""
    return int(json_fingerprint({"budget": budget_id})[:8], 16) % (2**31)


def verification_fingerprint(
    session: Session, *, username: str, workflow: str, scope: str, payload: dict[str, Any]
) -> str:
    """Bind executable inputs to fresh deployment and provider-connection identities.

    Args:
        session: Database session used to resolve current account credential revisions.
        username: Authenticated owner, never a client-supplied identity.
        workflow: Request family, such as Anything or an interaction.
        scope: Evaluation or complete execution readiness.
        payload: Canonical public request before runtime-only injection.

    Returns:
        Digest only; credential values and authored input are never persisted here.
    """
    inputs = {
        key: value
        for key, value in payload.items()
        if key not in _COSMETIC_FIELDS
        and not key.startswith("_")
        and (scope != "evaluation" or key in _EVALUATION_FIELDS)
    }
    connections = session.execute(
        select(BillingProviderKeyModel.id, BillingProviderKeyModel.updated_at, BillingProviderKeyModel.status)
        .where(BillingProviderKeyModel.username == username)
        .order_by(BillingProviderKeyModel.id)
    ).all()
    managed = settings.openrouter_api_key.get_secret_value() if settings.openrouter_api_key else None
    return json_fingerprint(
        {
            "contract": 1,
            "workflow": workflow,
            "scope": scope,
            "inputs": inputs,
            "deployment": {
                "code": settings.code_version,
                "scorer_image": settings.vercel_sandbox_image,
                "managed_route": json_fingerprint(managed),
                "sandbox_account": [settings.vercel_team_id, settings.vercel_project_id],
            },
            "connections": [[row.id, str(row.updated_at), row.status] for row in connections],
        }
    )


def preflight_document(row: WizardPreflightModel) -> dict[str, Any]:
    """Return only public evidence fields, keeping account and credential identities private.

    Args:
        row: Persisted setup attempt with its actual results.

    Returns:
        Public checks and completed workflow outputs, including pending usage status.
    """
    status = "pending" if row.status == "running" else row.status
    pending_reason = row.result.get("pending_reason")
    if status == "pending" and not isinstance(pending_reason, dict):
        usage_pending = any(
            check.get("key") == "usage" and check.get("status") == "pending" for check in row.result.get("checks", [])
        )
        pending_reason = {
            "category": "usage_reconciliation" if usage_pending else "setup_incomplete",
            "message": (
                "Provider or sandbox usage is awaiting final confirmation."
                if usage_pending
                else "Setup checks are incomplete."
            ),
        }
    return {
        "id": row.id,
        "fingerprint": row.fingerprint,
        "status": status,
        "may_advance": status == "succeeded"
        or (status == "pending" and pending_reason.get("category") == "later_stage_dependency"),
        "checks": row.result.get("checks", []),
        **({"pending_reason": pending_reason} if status == "pending" else {}),
        **({"dependency_result": row.result["dependency_result"]} if "dependency_result" in row.result else {}),
        **({"scorer_result": row.result["scorer_result"]} if "scorer_result" in row.result else {}),
        **({"workflow_result": row.result["workflow_result"]} if "workflow_result" in row.result else {}),
        **({"interaction_result": row.result["interaction_result"]} if "interaction_result" in row.result else {}),
        **({"judge_result": row.result["judge_result"]} if "judge_result" in row.result else {}),
        **({"terminal": row.result["terminal"]} if isinstance(row.result.get("terminal"), dict) else {}),
        **({"environment": row.result["environment"]} if isinstance(row.result.get("environment"), dict) else {}),
    }


class PreflightStore:
    """Coordinate setup retries across replicas using the same budget's transaction lock."""

    def __init__(self, engine: Engine, *, lease_seconds: float = 120) -> None:
        """Bind setup ownership to the budget database and a renewable finite lease.

        Args:
            engine: Authoritative budget and setup database.
            lease_seconds: Owner lifetime between successful heartbeats.
        """
        if not math.isfinite(lease_seconds) or lease_seconds <= 0:
            raise ValueError("Setup ownership requires a finite positive lease.")
        self.engine = engine
        self.lease_seconds = lease_seconds
        self.budgets = BudgetService(engine=engine)

    @contextmanager
    def _transaction(self) -> Iterator[Session]:
        """Serialize SQLite claims and retain normal row locking on PostgreSQL.

        Yields:
            One committed transaction, rolled back on every failure.
        """
        with Session(self.engine) as session:
            try:
                if self.engine.dialect.name == "sqlite":
                    session.execute(text("BEGIN IMMEDIATE"))
                yield session
                session.commit()
            except BaseException:
                session.rollback()
                raise

    @staticmethod
    def _pending(row: WizardPreflightModel) -> None:
        """Mark an interrupted or waiting attempt without fabricating successful checks.

        Args:
            row: Setup attempt with no current executable owner.
        """
        row.status, row.claim_token, row.lease_expires_at = "pending", None, None
        row.result = {
            **row.result,
            "_retry_after_usage": True,
            "pending_reason": {
                "category": "usage_reconciliation",
                "message": "Interrupted setup is awaiting confirmed usage before retrying.",
            },
            "checks": [
                *[check for check in row.result.get("checks", []) if check.get("key") != "usage"],
                {
                    "key": "usage",
                    "status": "pending",
                    "message": "Interrupted setup is awaiting confirmed usage before retrying.",
                },
            ],
        }

    @staticmethod
    def _after_settlement(row: WizardPreflightModel) -> bool:
        """Promote only fully performed successful checks after their usage settles.

        Args:
            row: Finished pending attempt with no outstanding paid operations.

        Returns:
            True when existing evidence should be returned; False when execution is still required.
        """
        if row.result.get("_retry_after_usage"):
            return False
        checks = row.result.get("checks", [])
        performed = [check for check in checks if check.get("key") != "usage"]
        if not performed or any(check.get("status") == "failed" for check in performed):
            return False
        if any(check.get("status") != "succeeded" for check in performed):
            return True
        row.status = "succeeded"
        result = {
            key: value for key, value in row.result.items() if key not in {"pending_reason", "_retry_after_usage"}
        }
        row.result = {
            **result,
            "checks": [
                {"key": "usage", "status": "succeeded", "message": "Usage confirmed."}
                if check.get("key") == "usage"
                else check
                for check in checks
            ],
        }
        return True

    def claim(
        self,
        *,
        username: str,
        budget_id: str,
        revision: int,
        workflow: str,
        scope: str,
        payload: dict[str, Any],
        reuse_failed: bool = False,
        exclusive_scope: bool = False,
        session_id: str | None = None,
        carry: dict[str, Any] | None = None,
    ) -> PreflightClaim:
        """Reuse matching success or atomically claim a new verification attempt.

        Args:
            username: Authenticated account.
            budget_id: Draft spending authority for every paid check.
            revision: Revision the caller last acknowledged.
            workflow: Public request family.
            scope: Requested readiness scope.
            payload: Canonical verification inputs.
            reuse_failed: Preserve an explicit debug request's failed result on transport replay.
            exclusive_scope: Bind a debug request's idempotency scope to exactly one input fingerprint.
            session_id: Judge session (SKY-696) whose attached budget pays for the attempt.
            carry: Result keys, with their defaults, that a new attempt keeps from the
                previous attempt, also after a crash; ``None`` starts every attempt empty.

        Returns:
            Public evidence, private owner token when claimed, and authoritative generation.
        """
        with self._transaction() as session:
            self.budgets.get(budget_id, username, session=session)
            budget = session.get(ExecutionBudgetModel, budget_id)
            assert budget is not None
            # A judge session pays each call from the budget attached to its own run row.
            judged = session_id is not None and budget.job_id == session_id and budget.state == "attached"
            if budget.revision != revision or not (judged or (budget.job_id is None and budget.state == "open")):
                raise BudgetConflictError("The setup budget changed or is already attached to a run.")
            now = datetime.now(UTC)
            running = list(
                session.scalars(
                    select(WizardPreflightModel)
                    .where(WizardPreflightModel.budget_id == budget_id, WizardPreflightModel.status == "running")
                    .with_for_update()
                )
            )
            expired = [row for row in running if row.lease_expires_at is None or _utc(row.lease_expires_at) <= now]
            if any(row.execution_generation == budget.generation for row in expired):
                self.budgets.fence_generation(
                    budget_id, username, expected_generation=budget.generation, session=session
                )
            for previous in running:
                if previous in expired or previous.execution_generation != budget.generation:
                    self._pending(previous)
            fingerprint = verification_fingerprint(
                session, username=username, workflow=workflow, scope=scope, payload=payload
            )
            if scope == "evaluation":
                # An evaluation check runs its samples under the budget's limit, so a
                # pass under one limit says nothing about another. Execution evidence
                # keeps the bare digest, which launch re-derives in require_current.
                fingerprint = json_fingerprint({"inputs": fingerprint, "limit": [budget.total_cents, budget.uncapped]})
            if (
                exclusive_scope
                and session.scalar(
                    select(WizardPreflightModel.id)
                    .where(
                        WizardPreflightModel.budget_id == budget_id,
                        WizardPreflightModel.scope == scope,
                        WizardPreflightModel.fingerprint != fingerprint,
                    )
                    .limit(1)
                )
                is not None
            ):
                raise BudgetConflictError("This preview request key already belongs to different inputs.")
            row = session.execute(
                select(WizardPreflightModel).where(
                    WizardPreflightModel.budget_id == budget_id,
                    WizardPreflightModel.scope == scope,
                    WizardPreflightModel.fingerprint == fingerprint,
                )
            ).scalar_one_or_none()
            if row is not None and (
                row.status in {"succeeded", "running"} or (reuse_failed and row.status == "failed")
            ):
                return PreflightClaim(preflight_document(row), None, budget.generation)
            if row is None:
                row = WizardPreflightModel(
                    id=str(uuid4()),
                    username=username,
                    budget_id=budget_id,
                    workflow=workflow,
                    scope=scope,
                    fingerprint=fingerprint,
                    status="pending",
                    attempt=0,
                    result={},
                    started_at=datetime.now(UTC),
                )
                session.add(row)
            unresolved = session.scalar(
                select(ExecutionOperationModel.id)
                .where(
                    ExecutionOperationModel.budget_id == budget_id,
                    ExecutionOperationModel.state.in_(("dispatched", "pending")),
                    ExecutionOperationModel.generation < budget.generation,
                )
                .limit(1)
            )
            if unresolved is not None:
                self._pending(row)
                session.flush()
                return PreflightClaim(preflight_document(row), None, budget.generation)
            if row.status == "pending" and row.attempt > 0:
                unsettled = session.scalar(
                    select(ExecutionOperationModel.id)
                    .where(
                        ExecutionOperationModel.budget_id == budget_id,
                        ExecutionOperationModel.state.in_(("reserved", "dispatched", "pending")),
                    )
                    .limit(1)
                )
                if unsettled is not None:
                    return PreflightClaim(preflight_document(row), None, budget.generation)
                if self._after_settlement(row):
                    session.flush()
                    return PreflightClaim(preflight_document(row), None, budget.generation)
                if reuse_failed and any(
                    key in row.result for key in ("scorer_result", "workflow_result", "judge_error")
                ):
                    row.status = "failed"
                    session.flush()
                    return PreflightClaim(preflight_document(row), None, budget.generation)
            kept = {key: row.result.get(key, default) for key, default in (carry or {}).items()}
            row.attempt += 1
            row.status, row.result = "running", dict(kept)
            row.started_at, row.finished_at = now, None
            row.claim_token, row.execution_generation = str(uuid4()), budget.generation
            row.lease_expires_at = now + timedelta(seconds=self.lease_seconds)
            session.flush()
            return PreflightClaim(preflight_document(row), row.claim_token, budget.generation, row.attempt, kept)

    def renew(self, identity: str, *, claim_token: str) -> bool:
        """Extend only a live owner whose shared budget generation is still current.

        Args:
            identity: Setup attempt identity.
            claim_token: Private token granted by claim.

        Returns:
            True for a renewed owner or its already-recorded terminal outcome; False after ownership loss.
        """
        with self._transaction() as session:
            row = session.get(WizardPreflightModel, identity, with_for_update=True)
            now = datetime.now(UTC)
            if row is None or row.claim_token != claim_token:
                return False
            if row.status != "running":
                return row.status in {"succeeded", "failed", "pending"}
            if row.lease_expires_at is None or _utc(row.lease_expires_at) <= now:
                return False
            generation = session.scalar(
                select(ExecutionBudgetModel.generation).where(ExecutionBudgetModel.id == row.budget_id)
            )
            if generation != row.execution_generation:
                return False
            row.lease_expires_at = now + timedelta(seconds=self.lease_seconds)
            return True

    @contextmanager
    def heartbeat(self, identity: str, *, claim_token: str) -> Iterator[None]:
        """Keep an executing setup owner alive without reviving an expired claim.

        Args:
            identity: Claimed setup record.
            claim_token: Private current-owner token.

        Yields:
            Control to the paid setup and finalization body.

        Raises:
            BudgetConflictError: When the caller has lost its execution claim.
        """
        if not self.renew(identity, claim_token=claim_token):
            raise BudgetConflictError("The setup execution claim is no longer current.")
        stop = threading.Event()
        lost = threading.Event()

        def beat() -> None:
            """Renew while the owner is alive, recording storage failures as lost ownership."""
            while not stop.wait(min(30, self.lease_seconds / 3)):
                try:
                    if self.renew(identity, claim_token=claim_token):
                        continue
                except Exception:
                    pass
                lost.set()
                return

        thread = threading.Thread(target=beat, name="wizard-preflight-lease", daemon=True)
        thread.start()
        try:
            yield
            if lost.is_set():
                raise BudgetConflictError("The setup execution claim was lost while checks were running.")
        finally:
            stop.set()
            thread.join(timeout=min(5, self.lease_seconds))

    def finish(self, identity: str, *, claim_token: str, status: str, result: dict[str, Any]) -> dict[str, Any]:
        """Record a completed attempt without claiming success for pending provider usage.

        Args:
            identity: Preflight identity owned by the claim winner.
            claim_token: Exact owner token; stale callers cannot overwrite newer evidence.
            status: Succeeded, failed, or pending.
            result: Scoped checks and optional actual scorer result.

        Returns:
            The persisted public evidence document.
        """
        if status not in {"succeeded", "failed", "pending"}:
            raise ValueError("Invalid setup outcome.")
        with self._transaction() as session:
            owner = session.execute(
                select(WizardPreflightModel.budget_id, WizardPreflightModel.username).where(
                    WizardPreflightModel.id == identity
                )
            ).first()
            if owner is None:
                raise BudgetNotFoundError("The setup attempt was not found.")
            budget = self.budgets.get(owner.budget_id, owner.username, session=session)
            row = session.get(WizardPreflightModel, identity, with_for_update=True)
            if (
                row is None
                or row.status != "running"
                or row.claim_token != claim_token
                or row.execution_generation != budget.generation
                or row.lease_expires_at is None
                or _utc(row.lease_expires_at) <= datetime.now(UTC)
            ):
                raise BudgetConflictError("The setup attempt is no longer owned.")
            checks = result.get("checks")
            if status == "succeeded" and (
                not isinstance(checks, list)
                or not checks
                or any(not isinstance(check, dict) or check.get("status") != "succeeded" for check in checks)
            ):
                raise BudgetConflictError("Setup success requires completed verification evidence.")
            if budget.pending_operations:
                status = "pending"
                checks = list(result.get("checks", []))
                if not any(check.get("key") == "usage" and check.get("status") == "pending" for check in checks):
                    checks.append(
                        {
                            "key": "usage",
                            "status": "pending",
                            "message": "Provider or sandbox usage is awaiting final confirmation.",
                        }
                    )
                result = {
                    **result,
                    "checks": checks,
                    "pending_reason": {
                        "category": "usage_reconciliation",
                        "message": "Provider or sandbox usage is awaiting final confirmation.",
                    },
                }
            row.status, row.result, row.finished_at = status, result, datetime.now(UTC)
            row.lease_expires_at = None
            session.flush()
            return preflight_document(row)

    def require_current(
        self,
        *,
        username: str,
        budget_id: str,
        identity: str | None,
        fingerprint: str | None,
        workflow: str,
        payload: dict[str, Any],
        session: Session | None = None,
    ) -> None:
        """Reject missing, stale, foreign, or incomplete evidence immediately before attachment.

        Args:
            username: Authenticated submitter.
            budget_id: Budget being attached to the new job.
            identity: Evidence identity returned by the server.
            fingerprint: Client's last acknowledged evidence digest.
            workflow: Exact request family.
            payload: Canonical current submission.
            session: Optional attachment transaction for atomic validation.
        """
        if session is None:
            with Session(self.engine) as owned:
                self.require_current(
                    username=username,
                    budget_id=budget_id,
                    identity=identity,
                    fingerprint=fingerprint,
                    workflow=workflow,
                    payload=payload,
                    session=owned,
                )
                return
        row = session.get(WizardPreflightModel, identity) if identity else None
        current = verification_fingerprint(
            session, username=username, workflow=workflow, scope="execution", payload=payload
        )
        if (
            row is None
            or row.username != username
            or row.budget_id != budget_id
            or row.scope != "execution"
            or row.workflow != workflow
            or row.status != "succeeded"
            or row.fingerprint != fingerprint
            or row.fingerprint != current
        ):
            raise BudgetConflictError("Run setup checks again for the current configuration before submitting.")
