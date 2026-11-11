"""Admin dashboard: the figures an owner needs to run Skynet day to day. [INTERNAL]

``GET /admin/dashboard`` rolls signups, activation, active users, runs,
failure reasons, money, served programs and product events over a trailing
window. It is admin-only; internal and test accounts (``users.is_internal``)
are excluded from every per-user figure.
"""

from __future__ import annotations

from collections import Counter, defaultdict
from datetime import UTC, date, datetime, timedelta
from typing import Annotated

from fastapi import APIRouter, Depends, Query
from pydantic import BaseModel
from sqlalchemy import func, or_, select
from sqlalchemy.orm import Session

from ...constants import PAYLOAD_OVERVIEW_NAME
from ...models.common import OptimizationStatus
from ...storage.models import JobModel, TelemetryEventModel, UserModel, WalletLedgerModel
from ..auth import AuthenticatedUser, get_authenticated_user, require_admin_user
from .serve import SERVE_INFERENCE_EVENT

AuthenticatedUserDep = Annotated[AuthenticatedUser, Depends(get_authenticated_user)]

TOP_LIMIT = 10
RUN_FAILED_EVENT = "run_failed"
# Clawbacks are negative rows too, but they reverse a top-up rather than
# charge for usage, so they stay out of the usage total. A closure row
# forfeits a deleted account's balance (SKY-567); it is not usage either.
_NON_USAGE_DEBITS = ("refund", "dispute", "closure")


class AdminDashboardTotals(BaseModel):
    """Window totals for the KPI cards."""

    signups: int
    activated: int
    active_users: int
    runs_started: int
    runs_succeeded: int
    runs_failed: int
    revenue_cents: int
    usage_cents: int
    serve_calls: int
    serve_errors: int


class AdminDashboardDay(BaseModel):
    """One UTC day of the trend charts."""

    day: date
    signups: int
    activated: int
    active_users: int
    runs_succeeded: int
    runs_failed: int
    runs_other: int


class AdminFailureReason(BaseModel):
    """Failed runs sharing one failure stage and exception class."""

    stage: str
    error_class: str | None
    count: int


class AdminTopProgram(BaseModel):
    """A served program ranked by calls in the window."""

    optimization_id: str
    name: str | None
    owner: str | None
    calls: int
    errors: int


class AdminEventCount(BaseModel):
    """A product event name with its count and distinct signed-in users."""

    name: str
    count: int
    users: int


class AdminDashboardResponse(BaseModel):
    """Everything the admin dashboard draws for the trailing ``days`` UTC days."""

    days: int
    totals: AdminDashboardTotals
    daily: list[AdminDashboardDay]
    failure_reasons: list[AdminFailureReason]
    top_programs: list[AdminTopProgram]
    top_events: list[AdminEventCount]


def _day(value: datetime) -> date:
    """Return the UTC calendar day of a timestamp.

    SQLite hands back naive datetimes, which are stored as UTC.

    Args:
        value: A stored timestamp.

    Returns:
        Its UTC date.
    """
    if value.tzinfo is None:
        return value.date()
    return value.astimezone(UTC).date()


def read_dashboard(session: Session, *, days: int, now: datetime) -> AdminDashboardResponse:
    """Aggregate the admin dashboard over the trailing ``days`` UTC days.

    Day buckets are filled in Python rather than with a SQL date function so
    the same code reads Postgres and the SQLite test stores identically.

    Args:
        session: An open ORM session.
        days: Window length in days, including today.
        now: The current UTC time.

    Returns:
        The zero-filled dashboard, days oldest first.
    """
    today = now.astimezone(UTC).date()
    first_day = today - timedelta(days=days - 1)
    cutoff = datetime(first_day.year, first_day.month, first_day.day, tzinfo=UTC)
    internal = set(session.scalars(select(UserModel.email).where(UserModel.is_internal.is_(True))))

    signups: Counter[date] = Counter(
        _day(created)
        for created, email in session.execute(
            select(UserModel.created_at, UserModel.email).where(UserModel.created_at >= cutoff)
        )
        if email not in internal
    )

    first_success = (
        select(JobModel.username, func.min(JobModel.completed_at).label("first_at"))
        .where(
            JobModel.status == OptimizationStatus.success.value,
            JobModel.parent_optimization_id.is_(None),
            JobModel.username.is_not(None),
            JobModel.completed_at.is_not(None),
        )
        .group_by(JobModel.username)
        .subquery()
    )
    activated: Counter[date] = Counter(
        _day(first_at)
        for username, first_at in session.execute(
            select(first_success.c.username, first_success.c.first_at).where(first_success.c.first_at >= cutoff)
        )
        if username not in internal
    )

    active_by_day: dict[date, set[str]] = defaultdict(set)
    active_users: set[str] = set()
    for username, received in session.execute(
        select(TelemetryEventModel.username, TelemetryEventModel.received_at).where(
            TelemetryEventModel.received_at >= cutoff, TelemetryEventModel.username.is_not(None)
        )
    ):
        if username in internal:
            continue
        active_by_day[_day(received)].add(username)
        active_users.add(username)

    succeeded: Counter[date] = Counter()
    failed: Counter[date] = Counter()
    other: Counter[date] = Counter()
    for status, created, username in session.execute(
        select(JobModel.status, JobModel.created_at, JobModel.username).where(
            JobModel.created_at >= cutoff, JobModel.parent_optimization_id.is_(None)
        )
    ):
        if username in internal:
            continue
        bucket = {OptimizationStatus.success.value: succeeded, OptimizationStatus.failed.value: failed}.get(
            status, other
        )
        bucket[_day(created)] += 1

    revenue = 0
    usage = 0
    for kind, delta, username in session.execute(
        select(WalletLedgerModel.kind, WalletLedgerModel.delta_cents, WalletLedgerModel.username).where(
            WalletLedgerModel.created_at >= cutoff
        )
    ):
        if username in internal:
            continue
        if kind == "topup":
            revenue += int(delta)
        elif delta < 0 and kind not in _NON_USAGE_DEBITS:
            usage -= int(delta)

    reasons: Counter[tuple[str, str | None]] = Counter()
    for properties, username in session.execute(
        select(TelemetryEventModel.properties, TelemetryEventModel.username).where(
            TelemetryEventModel.received_at >= cutoff, TelemetryEventModel.event_name == RUN_FAILED_EVENT
        )
    ):
        if username in internal:
            continue
        props = properties if isinstance(properties, dict) else {}
        reasons[(str(props.get("failure_stage") or "unknown"), props.get("error_class"))] += 1

    calls: Counter[str] = Counter()
    errors: Counter[str] = Counter()
    for properties in session.scalars(
        select(TelemetryEventModel.properties).where(
            TelemetryEventModel.received_at >= cutoff, TelemetryEventModel.event_name == SERVE_INFERENCE_EVENT
        )
    ):
        props = properties if isinstance(properties, dict) else {}
        optimization_id = props.get("optimization_id")
        if not isinstance(optimization_id, str):
            continue
        calls[optimization_id] += 1
        if props.get("status") == "error":
            errors[optimization_id] += 1
    owners: dict[str, tuple[str | None, str | None]] = {}
    if calls:
        for optimization_id, username, overview in session.execute(
            select(JobModel.optimization_id, JobModel.username, JobModel.payload_overview).where(
                JobModel.optimization_id.in_(list(calls))
            )
        ):
            name = overview.get(PAYLOAD_OVERVIEW_NAME) if isinstance(overview, dict) else None
            owners[optimization_id] = (username, name if isinstance(name, str) else None)
    programs = [
        AdminTopProgram(
            optimization_id=optimization_id,
            name=owners.get(optimization_id, (None, None))[1],
            owner=owners.get(optimization_id, (None, None))[0],
            calls=count,
            errors=errors[optimization_id],
        )
        for optimization_id, count in calls.most_common()
        if owners.get(optimization_id, (None, None))[0] not in internal
    ]

    event_rows = session.execute(
        select(
            TelemetryEventModel.event_name,
            func.count(TelemetryEventModel.id),
            func.count(func.distinct(TelemetryEventModel.username)),
        )
        .where(
            TelemetryEventModel.received_at >= cutoff,
            or_(TelemetryEventModel.username.is_(None), TelemetryEventModel.username.not_in(internal)),
        )
        .group_by(TelemetryEventModel.event_name)
        .order_by(func.count(TelemetryEventModel.id).desc())
        .limit(TOP_LIMIT)
    ).all()

    window = [first_day + timedelta(days=offset) for offset in range(days)]
    return AdminDashboardResponse(
        days=days,
        totals=AdminDashboardTotals(
            signups=sum(signups.values()),
            activated=sum(activated.values()),
            active_users=len(active_users),
            runs_started=sum(succeeded.values()) + sum(failed.values()) + sum(other.values()),
            runs_succeeded=sum(succeeded.values()),
            runs_failed=sum(failed.values()),
            revenue_cents=revenue,
            usage_cents=usage,
            serve_calls=sum(program.calls for program in programs),
            serve_errors=sum(program.errors for program in programs),
        ),
        daily=[
            AdminDashboardDay(
                day=day,
                signups=signups[day],
                activated=activated[day],
                active_users=len(active_by_day.get(day, ())),
                runs_succeeded=succeeded[day],
                runs_failed=failed[day],
                runs_other=other[day],
            )
            for day in window
        ],
        failure_reasons=[
            AdminFailureReason(stage=stage, error_class=error_class, count=count)
            for (stage, error_class), count in reasons.most_common(TOP_LIMIT)
        ],
        top_programs=programs[:TOP_LIMIT],
        top_events=[
            AdminEventCount(name=name, count=int(count), users=int(users)) for name, count, users in event_rows
        ],
    )


def create_admin_dashboard_router(*, job_store) -> APIRouter:
    """Build the admin dashboard router.

    Args:
        job_store: Storage backend exposing the SQLAlchemy ``engine``.

    Returns:
        A configured :class:`APIRouter`.
    """
    router = APIRouter()

    @router.get(
        "/admin/dashboard",
        response_model=AdminDashboardResponse,
        summary="Admin: signups, activation, runs, money, programs and events over a window",
    )
    def get_dashboard(
        current_user: AuthenticatedUserDep,
        days: Annotated[int, Query(ge=1, le=365)] = 30,
    ) -> AdminDashboardResponse:
        """Return the admin dashboard for the trailing ``days`` UTC days.

        Args:
            current_user: Authenticated caller; must be a backend admin.
            days: Window length, clamped to ``1..365``.

        Returns:
            An :class:`AdminDashboardResponse`.

        Raises:
            DomainError: 403 when the caller is not a backend admin.
        """
        require_admin_user(current_user)
        with Session(job_store.engine) as session:
            return read_dashboard(session, days=days, now=datetime.now(UTC))

    return router
