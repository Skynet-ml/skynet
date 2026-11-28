"""Exercise the north-star metric views against an explicitly supplied disposable PostgreSQL database.

Skipped unless ``APP_BUDGET_TEST_DB_URL`` points at Postgres, like
``test_budgets_postgres.py``. Each test runs in its own schema with only the
``users``, ``jobs`` and ``telemetry_events`` tables, seeds a two-week story and
reads it back through the views the migration creates.
"""

from __future__ import annotations

import importlib.util
from collections.abc import Iterator
from datetime import UTC, date, datetime, timedelta
from pathlib import Path
from types import ModuleType
from typing import Any
from uuid import uuid4

import pytest
from alembic.migration import MigrationContext
from alembic.operations import Operations
from sqlalchemy import Engine, create_engine, text
from sqlalchemy.orm import Session

from core.api.routers.telemetry import METRIC_VIEW_DDL, read_north_star
from core.config import app_env
from core.storage.models import AgentConversationModel, Base, JobModel, TelemetryEventModel, UserModel

TEST_DB_URL = app_env("APP_BUDGET_TEST_DB_URL")
pytestmark = pytest.mark.skipif(
    not TEST_DB_URL or not TEST_DB_URL.startswith("postgresql"),
    reason="Set APP_BUDGET_TEST_DB_URL to an explicitly disposable PostgreSQL database.",
)

_VERSIONS = Path(__file__).resolve().parents[3] / "alembic" / "versions"
_MIGRATION = _VERSIONS / "d1e2f3a4b5c6_add_metric_views.py"
_DROP_IN_USE_MIGRATION = _VERSIONS / "6db6fa226aca_drop_lifted_in_use_view.py"

WEEK_1 = date(2026, 9, 7)
WEEK_2 = date(2026, 9, 14)
SIGNUP_1 = datetime(2026, 9, 8, 10, tzinfo=UTC)
SIGNUP_2 = datetime(2026, 9, 15, 10, tzinfo=UTC)
TODAY = date(2026, 9, 17)

_LIFT = {"lift": 0.03, "held_out": True, "held_out_rows": 25, "from_sample": False}


@pytest.fixture
def database() -> Iterator[Engine]:
    """Isolate each test in its own schema without touching pre-existing data."""
    schema = f"metric_test_{uuid4().hex}"
    admin = create_engine(TEST_DB_URL)
    with admin.begin() as connection:
        connection.execute(text(f'CREATE SCHEMA "{schema}"'))
    engine = create_engine(TEST_DB_URL, connect_args={"options": f"-csearch_path={schema}"})
    try:
        Base.metadata.create_all(
            engine,
            tables=[
                UserModel.__table__,
                AgentConversationModel.__table__,
                JobModel.__table__,
                TelemetryEventModel.__table__,
            ],
        )
        yield engine
    finally:
        engine.dispose()
        with admin.begin() as connection:
            connection.execute(text(f'DROP SCHEMA "{schema}" CASCADE'))
        admin.dispose()


def _migration(path: Path = _MIGRATION) -> ModuleType:
    """Load a migration module from its file.

    Args:
        path: The migration file, the metric-views migration by default.

    Returns:
        The executed migration module.
    """
    spec = importlib.util.spec_from_file_location(f"migration_fixture_{path.stem}", path)
    assert spec is not None
    assert spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def _run_migration(engine: Engine, step: str, path: Path = _MIGRATION) -> None:
    """Run a migration's ``upgrade`` or ``downgrade`` on ``engine``.

    Args:
        engine: The per-test schema engine.
        step: ``"upgrade"`` or ``"downgrade"``.
        path: The migration file, the metric-views migration by default.
    """
    migration = _migration(path)
    with engine.begin() as connection, Operations.context(MigrationContext.configure(connection)):
        getattr(migration, step)()


def _view_names(engine: Engine) -> set[str]:
    """Return which metric views exist in the test schema.

    Args:
        engine: The per-test schema engine.

    Returns:
        The names of the metric views that resolve.
    """
    with engine.connect() as connection:
        return {
            name
            for name in METRIC_VIEW_DDL
            if connection.execute(text("SELECT to_regclass(:name)"), {"name": name}).scalar() is not None
        }


def _user(session: Session, email: str, created_at: datetime, *, internal: bool = False) -> None:
    """Add one account row.

    Args:
        session: Open ORM session.
        email: Account email, which is also its username.
        created_at: Signup time.
        internal: Whether the account is excluded from metrics.
    """
    session.add(UserModel(email=email, name=email, password_hash="", created_at=created_at, is_internal=internal))


def _run(session: Session, username: str, at: datetime, **overrides: Any) -> None:
    """Add one ``run_completed`` event, qualifying unless overridden.

    Args:
        session: Open ORM session.
        username: Run owner.
        at: Server receive time.
        **overrides: Property values replacing the qualifying defaults.
    """
    session.add(
        TelemetryEventModel(
            event_name="run_completed", received_at=at, username=username, properties={**_LIFT, **overrides}
        )
    )


def _seed(engine: Engine) -> None:
    """Seed the two-week story every assertion below reads.

    Args:
        engine: The per-test schema engine.
    """
    hour = timedelta(hours=1)
    with Session(engine) as session:
        for name in ("good", "low", "few", "sample", "late", "second", "internal"):
            _user(session, f"{name}@example.com", SIGNUP_1, internal=name == "internal")
        _user(session, "fresh@example.com", SIGNUP_2)
        _run(session, "good@example.com", SIGNUP_1 + 10 * hour)
        _run(session, "low@example.com", SIGNUP_1 + 10 * hour, lift=0.01)
        _run(session, "few@example.com", SIGNUP_1 + 10 * hour, held_out_rows=19)
        _run(session, "sample@example.com", SIGNUP_1 + 10 * hour, from_sample=True)
        _run(session, "late@example.com", SIGNUP_1 + 49 * hour)
        _run(session, "second@example.com", SIGNUP_1 + hour, lift=0.0)
        _run(session, "second@example.com", SIGNUP_1 + 2 * hour)
        _run(session, "internal@example.com", SIGNUP_1 + hour)
        _run(session, "good@example.com", SIGNUP_2)
        _run(session, "fresh@example.com", SIGNUP_2 + 5 * hour)
        session.commit()


def _read(engine: Engine) -> dict[str, dict[date, dict[str, Any]]]:
    """Read the endpoint's series for the three weeks up to :data:`TODAY`, keyed by week.

    Args:
        engine: The per-test schema engine.

    Returns:
        Each series as ``{week: row}``.
    """
    with engine.begin() as connection:
        body = read_north_star(connection, weeks=3, today=TODAY).model_dump()
    return {
        "first_lifts": {row["signup_week"]: row for row in body["first_lifts"]},
        "lifted_users": {row["week"]: row for row in body["lifted_users"]},
    }


def test_first_lifts_count_only_a_qualifying_first_run_within_48h(database: Engine) -> None:
    """Only the 0.03-lift, 25-row, +10h first run counts; every disqualifier and the internal user do not."""
    _seed(database)
    _run_migration(database, "upgrade")
    first = _read(database)["first_lifts"]
    assert first[WEEK_1] == {"signup_week": WEEK_1, "signups": 6, "first_lifts": 1}
    assert first[WEEK_2] == {"signup_week": WEEK_2, "signups": 1, "first_lifts": 1}


def test_lifted_users_split_new_and_returning_across_weeks(database: Engine) -> None:
    """A user lifted in week 1 and again in week 2 is new then returning; a week-2 first lifter is new."""
    _seed(database)
    _run_migration(database, "upgrade")
    lifted = _read(database)["lifted_users"]
    assert lifted[WEEK_1] == {"week": WEEK_1, "new_lifted": 3, "returning_lifted": 0}
    assert lifted[WEEK_2] == {"week": WEEK_2, "new_lifted": 1, "returning_lifted": 1}


def test_series_are_newest_first_and_zero_filled(database: Engine) -> None:
    """Every series has one row per requested week, newest first, including empty weeks."""
    _run_migration(database, "upgrade")
    with database.begin() as connection:
        body = read_north_star(connection, weeks=3, today=TODAY)
    weeks = [WEEK_2, WEEK_1, WEEK_1 - timedelta(weeks=1)]
    assert [row.signup_week for row in body.first_lifts] == weeks
    assert [row.week for row in body.lifted_users] == weeks


def test_migration_downgrade_drops_the_views(database: Engine) -> None:
    """Upgrade creates every router view and downgrade removes them."""
    _run_migration(database, "upgrade")
    assert _view_names(database) == set(METRIC_VIEW_DDL)
    _run_migration(database, "downgrade")
    assert _view_names(database) == set()


def test_read_creates_missing_views_on_an_unmigrated_database(database: Engine) -> None:
    """A database built by create_all and stamped at head still gets the views on first read."""
    _seed(database)
    assert _view_names(database) == set()
    assert _read(database)["first_lifts"][WEEK_1]["first_lifts"] == 1
    assert _view_names(database) == set(METRIC_VIEW_DDL)


def test_drop_in_use_migration_drops_and_restores_the_view(database: Engine) -> None:
    """The later migration drops the lifted-in-use view and its downgrade restores it."""
    exists = "SELECT to_regclass('metric_lifted_in_use_weekly')"
    _run_migration(database, "upgrade")
    _run_migration(database, "upgrade", _DROP_IN_USE_MIGRATION)
    with database.connect() as connection:
        assert connection.execute(text(exists)).scalar() is None
    _run_migration(database, "downgrade", _DROP_IN_USE_MIGRATION)
    with database.connect() as connection:
        assert connection.execute(text(exists)).scalar() is not None
