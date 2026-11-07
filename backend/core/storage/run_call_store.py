"""Persistence seam for the run record: every relay call a candidate makes (SKY-364).

The relay calls :meth:`PostgresRunCallStore.record_call` once for each call
out of a box. The store removes the given secret values from the bodies,
cuts bodies over :data:`BODY_CAP_BYTES`, and writes one ``run_calls`` row.
``run_candidates`` holds each candidate's role and lifecycle state. The live
read routes page through a candidate's calls with an ``id`` cursor.
"""

from __future__ import annotations

import json
from datetime import UTC, datetime
from typing import Any

from sqlalchemy import func, select
from sqlalchemy.engine import Engine
from sqlalchemy.exc import IntegrityError
from sqlalchemy.orm import Session

from .models import RunCallModel, RunCandidateModel

BODY_CAP_BYTES = 64 * 1024
SECRET_MASK = "[REDACTED]"
CALL_KINDS = frozenset({"model", "mcp", "stage", "evaluator", "host"})
CANDIDATE_STATES = frozenset({"running", "done", "failed"})


def _iso(value: datetime | None) -> str | None:
    """Return a stored timestamp as an aware ISO string; SQLite hands back naive UTC."""
    if value is None:
        return None
    if value.tzinfo is None:
        value = value.replace(tzinfo=UTC)
    return value.isoformat()


def _parse_timestamp(value: Any) -> datetime:
    """Return a call's start time as an aware datetime; now when absent or unreadable."""
    if isinstance(value, datetime):
        return value if value.tzinfo is not None else value.replace(tzinfo=UTC)
    if isinstance(value, str):
        try:
            parsed = datetime.fromisoformat(value)
        except ValueError:
            return datetime.now(UTC)
        return parsed if parsed.tzinfo is not None else parsed.replace(tzinfo=UTC)
    return datetime.now(UTC)


def strip_secrets(text: str, secrets: list[str]) -> str:
    """Replace every occurrence of each secret value with a mask.

    Each secret is also matched in its JSON-escaped form, because bodies are
    mostly JSON and a server that echoes a header escapes quotes, backslashes
    and non-ASCII characters. Longer forms go first, so a secret that contains
    a shorter one is masked whole instead of leaving its remainder.

    Args:
        text: The body to clean.
        secrets: Secret values to remove; empty values are ignored.

    Returns:
        The body with no secret value left in it.
    """
    forms = {value for value in secrets if value}
    forms |= {json.dumps(value)[1:-1] for value in forms}
    for form in sorted(forms, key=len, reverse=True):
        text = text.replace(form, SECRET_MASK)
    return text


def cut_body(text: str, cap: int = BODY_CAP_BYTES) -> tuple[str, int, bool]:
    """Cut a body to at most ``cap`` UTF-8 bytes.

    Args:
        text: The body, already free of secrets.
        cap: The byte cap.

    Returns:
        ``(stored_text, full_size_bytes, truncated)``.
    """
    encoded = text.encode("utf-8")
    if len(encoded) <= cap:
        return text, len(encoded), False
    # errors="ignore" drops a multi-byte character the cap splits in two.
    return encoded[:cap].decode("utf-8", errors="ignore"), len(encoded), True


class PostgresRunCallStore:
    """Relay call records in ``run_calls`` and candidate states in ``run_candidates``."""

    def __init__(self, engine: Engine) -> None:
        """Bind the store to a SQLAlchemy engine.

        Args:
            engine: Engine whose schema carries ``run_calls`` and ``run_candidates``.
        """
        self._engine = engine

    def record_call(self, optimization_id: str, call: dict[str, Any], secrets: list[str]) -> int:
        """Write one relay call, with its secrets removed and its bodies cut.

        Secrets are removed before the cut, so a cut cannot split a secret and
        leave part of it in the stored body. The candidate is marked
        ``running`` when it has no state yet.

        Args:
            optimization_id: Owning job id.
            call: The call: ``candidate_id``, ``role``, ``kind``, ``target``,
                ``started_at`` (ISO string or datetime), ``duration_ms``,
                ``status``, ``failure_reason``, ``cost_usd``, ``request`` and
                ``response``.
            secrets: Every secret value the call could carry, including the
                resolved values of the user's ``${NAME}`` placeholders.

        Returns:
            The new row's id, which is also its cursor.

        Raises:
            ValueError: When ``kind`` is not a known call kind.
        """
        kind = str(call.get("kind") or "")
        if kind not in CALL_KINDS:
            raise ValueError(f"unknown run call kind: {kind!r}")
        request, request_bytes, request_truncated = cut_body(strip_secrets(str(call.get("request") or ""), secrets))
        response, response_bytes, response_truncated = cut_body(strip_secrets(str(call.get("response") or ""), secrets))
        failure_reason = call.get("failure_reason")
        candidate_id = str(call["candidate_id"])[:64]
        role = str(call.get("role") or "")[:64]
        fields = {
            "optimization_id": optimization_id,
            "candidate_id": candidate_id,
            "role": role,
            "kind": kind,
            "target": strip_secrets(str(call.get("target") or ""), secrets)[:512],
            "started_at": _parse_timestamp(call.get("started_at")),
            "duration_ms": call.get("duration_ms"),
            "status": str(call.get("status") or "")[:16],
            "failure_reason": None if failure_reason is None else strip_secrets(str(failure_reason), secrets),
            "cost_usd": call.get("cost_usd"),
            "request": request,
            "request_bytes": request_bytes,
            "request_truncated": request_truncated,
            "response": response,
            "response_bytes": response_bytes,
            "response_truncated": response_truncated,
        }
        with Session(self._engine) as session:
            row = RunCallModel(**fields)
            session.add(row)
            if session.get(RunCandidateModel, (optimization_id, candidate_id)) is None:
                session.add(
                    RunCandidateModel(
                        optimization_id=optimization_id, candidate_id=candidate_id, role=role, state="running"
                    )
                )
            try:
                session.commit()
            except IntegrityError:
                # A parallel call of the same new candidate inserted its state
                # row first; keep that row and write only this call.
                session.rollback()
                row = RunCallModel(**fields)
                session.add(row)
                session.commit()
            return int(row.id)

    def set_candidate_state(self, optimization_id: str, candidate_id: str, role: str, state: str) -> None:
        """Insert or update one candidate's role and lifecycle state.

        Args:
            optimization_id: Owning job id.
            candidate_id: The candidate within the run.
            role: The candidate's role or stage.
            state: ``running``, ``done`` or ``failed``.

        Raises:
            ValueError: When ``state`` is not a known candidate state.
        """
        if state not in CANDIDATE_STATES:
            raise ValueError(f"unknown candidate state: {state!r}")
        candidate_id = str(candidate_id)[:64]
        with Session(self._engine) as session:
            existing = session.get(RunCandidateModel, (optimization_id, candidate_id))
            if existing is None:
                session.add(
                    RunCandidateModel(
                        optimization_id=optimization_id, candidate_id=candidate_id, role=role[:64], state=state
                    )
                )
            else:
                existing.role = role[:64] or existing.role
                existing.state = state
                existing.updated_at = datetime.now(UTC)
            session.commit()

    def list_calls(self, optimization_id: str, candidate_id: str, *, since: int = 0, limit: int = 100) -> list[dict]:
        """Return a candidate's calls after the ``since`` cursor, oldest first.

        Args:
            optimization_id: Owning job id.
            candidate_id: The candidate within the run.
            since: The last call id the caller holds; ``0`` for the start.
            limit: The most calls to return.

        Returns:
            The call records, in write order.
        """
        with Session(self._engine) as session:
            rows = session.scalars(
                select(RunCallModel)
                .where(
                    RunCallModel.optimization_id == optimization_id,
                    RunCallModel.candidate_id == candidate_id,
                    RunCallModel.id > since,
                )
                .order_by(RunCallModel.id)
                .limit(limit)
            ).all()
            return [self._call_record(row) for row in rows]

    def get_candidate(self, optimization_id: str, candidate_id: str) -> dict | None:
        """Return one candidate's summary, or ``None`` when it has no state and no calls.

        Args:
            optimization_id: Owning job id.
            candidate_id: The candidate within the run.

        Returns:
            The candidate summary, or ``None``.
        """
        return next(
            (c for c in self.list_candidates(optimization_id) if c["candidate_id"] == candidate_id),
            None,
        )

    def list_candidates(self, optimization_id: str) -> list[dict]:
        """Return the run's candidates with role, state, call count and cost so far.

        Args:
            optimization_id: Owning job id.

        Returns:
            One summary for each candidate, in start order.
        """
        with Session(self._engine) as session:
            candidates = session.scalars(
                select(RunCandidateModel)
                .where(RunCandidateModel.optimization_id == optimization_id)
                .order_by(RunCandidateModel.started_at, RunCandidateModel.candidate_id)
            ).all()
            totals = {
                candidate_id: (int(count), float(cost or 0.0))
                for candidate_id, count, cost in session.execute(
                    select(RunCallModel.candidate_id, func.count(RunCallModel.id), func.sum(RunCallModel.cost_usd))
                    .where(RunCallModel.optimization_id == optimization_id)
                    .group_by(RunCallModel.candidate_id)
                ).all()
            }
            return [
                {
                    "candidate_id": candidate.candidate_id,
                    "role": candidate.role,
                    "state": candidate.state,
                    "call_count": totals.get(candidate.candidate_id, (0, 0.0))[0],
                    "cost_usd": totals.get(candidate.candidate_id, (0, 0.0))[1],
                    "started_at": _iso(candidate.started_at),
                    "updated_at": _iso(candidate.updated_at),
                }
                for candidate in candidates
            ]

    @staticmethod
    def _call_record(row: RunCallModel) -> dict[str, Any]:
        """Project an ORM row onto the record shape the read routes serve."""
        return {
            "id": int(row.id),
            "candidate_id": row.candidate_id,
            "role": row.role,
            "kind": row.kind,
            "target": row.target,
            "started_at": _iso(row.started_at),
            "duration_ms": row.duration_ms,
            "status": row.status,
            "failure_reason": row.failure_reason,
            "cost_usd": row.cost_usd,
            "request": row.request,
            "request_bytes": row.request_bytes,
            "request_truncated": row.request_truncated,
            "response": row.response,
            "response_bytes": row.response_bytes,
            "response_truncated": row.response_truncated,
        }
