"""Tests for the admin dashboard router.

Mounts ``create_admin_dashboard_router`` on an in-memory SQLite store and
covers authorization, the empty window, every aggregate, and the exclusion of
internal accounts.
"""

from __future__ import annotations

from datetime import UTC, datetime, timedelta
from typing import Any

from fastapi import FastAPI
from fastapi.testclient import TestClient
from sqlalchemy import create_engine
from sqlalchemy.orm import Session, sessionmaker
from sqlalchemy.pool import StaticPool

from ...storage.models import Base, JobModel, TelemetryEventModel, UserModel, WalletLedgerModel
from ...storage.remote import RemoteDBJobStore
from ..auth import AuthenticatedUser, get_authenticated_user
from ..routers.admin_dashboard import create_admin_dashboard_router

_ADMIN = AuthenticatedUser(username="alice", role="admin", groups=())
_NONADMIN = AuthenticatedUser(username="bob", role="user", groups=())


class _MemStore(RemoteDBJobStore):
    """In-memory SQLite store for admin-dashboard tests (no pgvector)."""

    def __init__(self) -> None:
        """Build an in-memory SQLite engine and create the ORM tables."""
        self._engine = create_engine(
            "sqlite:///:memory:",
            connect_args={"check_same_thread": False},
            poolclass=StaticPool,
        )
        Base.metadata.create_all(self._engine)
        self._session_factory = sessionmaker(bind=self._engine)


def _client(user: AuthenticatedUser) -> tuple[TestClient, _MemStore]:
    """Mount the router on a fresh store, authenticated as ``user``.

    Args:
        user: The caller every request resolves to.

    Returns:
        A ``(client, store)`` pair.
    """
    store = _MemStore()
    app = FastAPI()
    app.include_router(create_admin_dashboard_router(job_store=store))
    app.dependency_overrides[get_authenticated_user] = lambda: user
    return TestClient(app), store


def _user(email: str, created: datetime, *, internal: bool = False) -> UserModel:
    """Build a user row.

    Args:
        email: Account identity.
        created: Signup time.
        internal: Whether the account is excluded from metrics.

    Returns:
        The unsaved row.
    """
    return UserModel(email=email, name=email, password_hash="", created_at=created, is_internal=internal)


def _job(job_id: str, username: str, status: str, created: datetime, **extra: Any) -> JobModel:
    """Build a top-level job row.

    Args:
        job_id: Optimization id.
        username: Owner.
        status: Job status.
        created: Submission time.
        **extra: Further column values.

    Returns:
        The unsaved row.
    """
    return JobModel(optimization_id=job_id, username=username, status=status, created_at=created, **extra)


def _event(name: str, username: str | None, received: datetime, **properties: Any) -> TelemetryEventModel:
    """Build a telemetry event row.

    Args:
        name: Event name.
        username: Signed-in identity, or ``None``.
        received: Server receive time.
        **properties: Event properties.

    Returns:
        The unsaved row.
    """
    return TelemetryEventModel(event_name=name, username=username, received_at=received, properties=properties)


def test_dashboard_forbidden_for_non_admin() -> None:
    """A non-admin caller gets 403."""
    client, _ = _client(_NONADMIN)
    assert client.get("/admin/dashboard").status_code == 403


def test_dashboard_empty_window_is_zero_filled() -> None:
    """With no data every total is zero and each day of the window is present."""
    client, _ = _client(_ADMIN)
    body = client.get("/admin/dashboard", params={"days": 7}).json()
    assert body["days"] == 7
    assert set(body["totals"].values()) == {0}
    assert len(body["daily"]) == 7
    assert body["daily"][-1]["day"] == datetime.now(UTC).date().isoformat()
    assert body["failure_reasons"] == []
    assert body["top_programs"] == []
    assert body["top_events"] == []


def test_dashboard_aggregates_and_excludes_internal_accounts() -> None:
    """Every figure counts real users in the window and skips internal ones."""
    client, store = _client(_ADMIN)
    now = datetime.now(UTC)
    old = now - timedelta(days=60)
    with Session(store.engine) as session:
        session.add_all(
            [
                _user("new@x.io", now),
                _user("old@x.io", old),
                _user("staff@x.io", now, internal=True),
                _job("j1", "new@x.io", "success", now, completed_at=now, payload_overview={"name": "Triage"}),
                _job("j2", "new@x.io", "failed", now),
                _job("j3", "old@x.io", "success", old, completed_at=old),
                _job("j4", "old@x.io", "running", now),
                _job("j5", "staff@x.io", "success", now, completed_at=now),
                _event("page_view", "new@x.io", now),
                _event("page_view", "staff@x.io", now),
                _event("page_view", None, now),
                _event("run_failed", "new@x.io", now, failure_stage="evaluate", error_class="ValueError"),
                _event("run_failed", "staff@x.io", now, failure_stage="sandbox", error_class="OSError"),
                _event("serve_inference", "new@x.io", now, optimization_id="j1", status="ok"),
                _event("serve_inference", "new@x.io", now, optimization_id="j1", status="error"),
                _event("serve_inference", "staff@x.io", now, optimization_id="j5", status="ok"),
                WalletLedgerModel(username="new@x.io", delta_cents=500, kind="topup", created_at=now),
                WalletLedgerModel(username="new@x.io", delta_cents=-120, kind="run", created_at=now),
                WalletLedgerModel(username="new@x.io", delta_cents=-50, kind="refund", created_at=now),
                WalletLedgerModel(username="new@x.io", delta_cents=-30, kind="closure", created_at=now),
                WalletLedgerModel(
                    username="new@x.io",
                    delta_cents=20,
                    kind="closure",
                    description="Forfeit reversed by refund",
                    created_at=now,
                ),
                WalletLedgerModel(username="staff@x.io", delta_cents=900, kind="topup", created_at=now),
            ]
        )
        session.commit()

    body = client.get("/admin/dashboard", params={"days": 30}).json()

    assert body["totals"] == {
        "signups": 1,
        "activated": 1,
        "active_users": 1,
        "runs_started": 3,
        "runs_succeeded": 1,
        "runs_failed": 1,
        "revenue_cents": 500,
        "usage_cents": 120,
        "serve_calls": 2,
        "serve_errors": 1,
    }
    today = body["daily"][-1]
    assert today["signups"] == 1
    assert (today["runs_succeeded"], today["runs_failed"], today["runs_other"]) == (1, 1, 1)
    assert body["failure_reasons"] == [{"stage": "evaluate", "error_class": "ValueError", "count": 1}]
    assert body["top_programs"] == [
        {"optimization_id": "j1", "name": "Triage", "owner": "new@x.io", "calls": 2, "errors": 1}
    ]
    events = {row["name"]: row for row in body["top_events"]}
    assert events["page_view"] == {"name": "page_view", "count": 2, "users": 1}


def test_dashboard_rejects_out_of_range_window() -> None:
    """The window is clamped to 1..365 days."""
    client, _ = _client(_ADMIN)
    assert client.get("/admin/dashboard", params={"days": 0}).status_code == 422
    assert client.get("/admin/dashboard", params={"days": 366}).status_code == 422
