"""Run and sharing events, turned into :func:`.dispatch.notify` calls.

Each helper gathers the event's facts (run name, scores, spend, duration)
and hands them to ``notify``, which stores the inbox item and routes it to the
user's channels. Every helper is safe to call unconditionally from the API and
worker layers: a missing engine, recipient or run is a silent no-op.
"""

from __future__ import annotations

import logging
from datetime import UTC, datetime
from typing import Any

from sqlalchemy.orm import Session

from ..constants import PAYLOAD_OVERVIEW_NAME
from ..i18n import t
from ..storage.models import JobModel
from .catalog import BUDGET_NEAR, FAILED, FINISHED, NEW_BEST, SHARED, STUCK
from .dispatch import notify
from .preferences import notification_engine

logger = logging.getLogger(__name__)

_ERROR_DETAIL_CHARS = 150


def _aware(value: datetime | None) -> datetime | None:
    """Return ``value`` as a UTC-aware datetime (SQLite returns naive ones).

    Args:
        value: A stored timestamp.

    Returns:
        The aware timestamp, or ``None``.
    """
    if value is None:
        return None
    return value if value.tzinfo else value.replace(tzinfo=UTC)


def _run_context(optimization_id: str) -> tuple[str, float | None]:
    """Look up a run's display name and how long it has run.

    Args:
        optimization_id: The run.

    Returns:
        ``(name, duration_seconds)``; the name falls back to a short id and
        the duration is ``None`` before the run started.
    """
    fallback = optimization_id[:8]
    engine = notification_engine()
    if engine is None:
        return fallback, None
    try:
        with Session(engine) as session:
            job = session.get(JobModel, optimization_id)
            if job is None:
                return fallback, None
            overview = job.payload_overview if isinstance(job.payload_overview, dict) else {}
            name = overview.get(PAYLOAD_OVERVIEW_NAME)
            started = _aware(job.started_at)
            ended = _aware(job.completed_at) or datetime.now(UTC)
    except Exception:
        logger.debug("Run context lookup failed for %s", optimization_id, exc_info=True)
        return fallback, None
    duration = (ended - started).total_seconds() if started else None
    return (name if isinstance(name, str) and name.strip() else fallback), duration


def _emit(
    username: str | None,
    event_type: str,
    optimization_id: str,
    facts: dict[str, Any],
    *,
    once: bool = True,
) -> None:
    """Enrich facts with the run's name and duration, then call ``notify``.

    Args:
        username: Recipient.
        event_type: Catalog event type.
        optimization_id: The run.
        facts: Event-specific facts.
        once: Whether a repeat of this event for this run is a no-op.
    """
    if not username:
        return
    name, duration = _run_context(optimization_id)
    facts.setdefault("duration", round(duration) if duration is not None else None)
    notify(
        username,
        event_type,
        optimization_id,
        facts,
        title=name,
        dedupe_key=f"{event_type}:{optimization_id}" if once else None,
    )


def _score_facts(baseline: float | None, score: float | None) -> dict[str, Any]:
    """Build the score and delta-vs-baseline facts.

    Args:
        baseline: The starting score, when known.
        score: The current or final score, when known.

    Returns:
        ``score`` and, when both are known, ``delta``.
    """
    facts: dict[str, Any] = {"score": score}
    if baseline is not None and score is not None:
        facts["delta"] = score - baseline
    return facts


def notify_job_completed(
    optimization_id: str,
    username: str,
    status: str,
    message: str | None = None,
    baseline_score: float | None = None,
    optimized_score: float | None = None,
) -> None:
    """Emit ``finished`` or ``failed`` for a run that reached a terminal status.

    A cancelled run emits nothing: the owner is the one who cancelled it.

    Args:
        optimization_id: The run.
        username: Owner and recipient.
        status: ``success``, ``stopped``, ``cancelled`` or ``failed``.
        message: Error message; shown (truncated) on ``failed``.
        baseline_score: Pre-optimization score.
        optimized_score: Final score.
    """
    if status in ("success", "stopped"):
        _emit(username, FINISHED, optimization_id, _score_facts(baseline_score, optimized_score))
    elif status == "failed":
        detail = message
        if detail and len(detail) > _ERROR_DETAIL_CHARS:
            detail = f"{detail[:_ERROR_DETAIL_CHARS]}..."
        _emit(username, FAILED, optimization_id, {"detail": detail})
    elif status != "cancelled":
        logger.warning("Skipping notification for unknown job status: %s", status)


def notify_run_new_best(optimization_id: str, username: str, score: float, baseline: float | None = None) -> None:
    """Emit ``new_best`` when a running optimization beats its starting score.

    Args:
        optimization_id: The run.
        username: Owner and recipient.
        score: The new best score.
        baseline: The score the run started from.
    """
    _emit(username, NEW_BEST, optimization_id, _score_facts(baseline, score))


def notify_run_stuck(optimization_id: str, username: str, best_score: float | None, stuck_fraction: float) -> None:
    """Emit ``stuck`` when a run's best score stalls over part of its budget.

    Args:
        optimization_id: The run.
        username: Owner and recipient.
        best_score: Best score so far.
        stuck_fraction: Fraction of the evaluation budget spent without improvement.
    """
    _emit(
        username,
        STUCK,
        optimization_id,
        {"score": best_score, "detail": t("notifier.body.stuck", percent=round(stuck_fraction * 100))},
    )


def notify_run_budget(optimization_id: str, username: str, spent_cents: float, limit_cents: int) -> None:
    """Emit ``budget_near`` when spend crosses the alert fraction of the limit.

    Args:
        optimization_id: The run.
        username: Owner and recipient.
        spent_cents: Cents settled so far.
        limit_cents: The run's spending limit in cents.
    """
    _emit(username, BUDGET_NEAR, optimization_id, {"spent": spent_cents / 100, "limit": limit_cents / 100})


def _emit_shared(optimization_id: str, recipient: str, actor: str, kind: str, detail: str, role: str | None) -> None:
    """Emit ``shared`` for an invite, a role change or an ownership transfer.

    Args:
        optimization_id: The shared run.
        recipient: The user whose access changed.
        actor: Who changed it.
        kind: ``invite``, ``role`` or ``transfer``.
        detail: Localized sentence for the email.
        role: The granted tier, when there is one.
    """
    _emit(
        recipient,
        SHARED,
        optimization_id,
        {"detail": detail, "actor": actor, "kind": kind, "role": role, "duration": None},
        once=False,
    )


def notify_share_invite(optimization_id: str, grantee: str, inviter: str, role: str) -> None:
    """Emit ``shared`` when a user is invited to a run.

    Args:
        optimization_id: The shared run.
        grantee: Invited user (the recipient).
        inviter: Who issued the invite.
        role: Tier granted (``viewer`` or ``editor``).
    """
    detail = t("notifier.share.invite.body", inviter=inviter, role=t(f"notifier.share.role.{role}"))
    _emit_shared(optimization_id, grantee, inviter, "invite", detail, role)


def notify_role_change(optimization_id: str, grantee: str, actor: str, role: str) -> None:
    """Emit ``shared`` when a member's access tier changes.

    Args:
        optimization_id: The run.
        grantee: Affected user (the recipient).
        actor: Who changed the role.
        role: New tier (``viewer`` or ``editor``).
    """
    detail = t("notifier.share.role_change.body", actor=actor, role=t(f"notifier.share.role.{role}"))
    _emit_shared(optimization_id, grantee, actor, "role", detail, role)


def notify_ownership_transfer(optimization_id: str, new_owner: str, actor: str) -> None:
    """Emit ``shared`` when a run's ownership moves to a user.

    Args:
        optimization_id: The run.
        new_owner: The new owner (the recipient).
        actor: Who transferred it.
    """
    detail = t("notifier.share.transfer.body", actor=actor)
    _emit_shared(optimization_id, new_owner, actor, "transfer", detail, None)
