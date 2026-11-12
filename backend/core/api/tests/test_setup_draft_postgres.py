"""Race the setup draft revision check on a real PostgreSQL row lock.

Runs only against an explicitly disposable database named by
``APP_BUDGET_TEST_DB_URL``, the same one the budget race tests use; each test
works in its own schema.
"""

from __future__ import annotations

import contextlib
import threading
from collections.abc import Iterator
from types import SimpleNamespace
from uuid import uuid4

import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient
from sqlalchemy import Engine, create_engine, text
from sqlalchemy.orm import Session

from ...config import app_env
from ...storage.models import SetupDraftModel
from ...storage.usage import StorageUsage
from ..auth import AuthenticatedUser, get_authenticated_user
from ..routers import setup_draft

TEST_DB_URL = app_env("APP_BUDGET_TEST_DB_URL")
pytestmark = pytest.mark.skipif(
    not TEST_DB_URL or not TEST_DB_URL.startswith("postgresql"),
    reason="Set APP_BUDGET_TEST_DB_URL to an explicitly disposable PostgreSQL database.",
)

_PATH = "/wizard/setup-draft"
_BODY = {"left_at": 1, "values": {"k": "seed"}, "chat": None, "staged_dataset_id": None}


@pytest.fixture
def database() -> Iterator[Engine]:
    """Give each race its own schema with only the setup draft table.

    Yields:
        An engine whose search path is the new schema.
    """
    schema = f"setup_draft_test_{uuid4().hex}"
    admin = create_engine(TEST_DB_URL)
    with admin.begin() as connection:
        connection.execute(text(f'CREATE SCHEMA "{schema}"'))
    engine = create_engine(TEST_DB_URL, connect_args={"options": f"-csearch_path={schema}"})
    try:
        SetupDraftModel.metadata.create_all(engine, tables=[SetupDraftModel.__table__])
        yield engine
    finally:
        engine.dispose()
        with admin.begin() as connection:
            connection.execute(text(f'DROP SCHEMA "{schema}" CASCADE'))
        admin.dispose()


def _app(engine: Engine) -> FastAPI:
    """Mount the setup draft router for ``alice`` with an unlimited budget.

    Args:
        engine: The test engine.

    Returns:
        The app.
    """
    job_store = SimpleNamespace(
        engine=engine,
        get_effective_user_storage_quota=lambda _username: 10**12,
        compute_user_storage=lambda _username: StorageUsage(total=0, breakdown={}),
    )
    app = FastAPI()
    app.include_router(setup_draft.create_setup_draft_router(job_store=job_store))
    app.dependency_overrides[get_authenticated_user] = lambda: AuthenticatedUser(
        username="alice", role="user", groups=()
    )
    return app


@pytest.mark.parametrize("second", ["put", "delete"])
def test_two_calls_from_one_revision_cannot_both_change_the_slot(
    database: Engine, monkeypatch: pytest.MonkeyPatch, second: str
) -> None:
    """Two calls that read the same revision at the same moment: one changes the slot, the other is stale."""
    app = _app(database)
    assert TestClient(app).put(_PATH, json=_BODY, params={"base_revision": 0}).json()["revision"] == 1
    # Both calls wait here after their locked read; without the lock both pass the check.
    barrier = threading.Barrier(2, timeout=1)

    class GatedSession(Session):
        """Session that holds each locked read until both calls made one."""

        def get(self, *args, **kwargs):
            """Read the row, then wait for the other call's read.

            Args:
                *args: Passed to ``Session.get``.
                **kwargs: Passed to ``Session.get``.

            Returns:
                The row, or ``None``.
            """
            row = super().get(*args, **kwargs)
            if "with_for_update" in kwargs:
                with contextlib.suppress(threading.BrokenBarrierError):
                    barrier.wait()
            return row

    monkeypatch.setattr(setup_draft, "Session", GatedSession)
    results: dict[str, dict] = {}

    def put() -> None:
        """Save from revision 1."""
        body = {**_BODY, "values": {"k": "first"}}
        results["put"] = TestClient(app).put(_PATH, json=body, params={"base_revision": 1}).json()

    def other() -> None:
        """Save or delete from revision 1."""
        client = TestClient(app)
        if second == "put":
            body = {**_BODY, "values": {"k": "second"}}
            results["other"] = client.put(_PATH, json=body, params={"base_revision": 1}).json()
        else:
            results["other"] = client.delete(_PATH, params={"revision": 1}).json()

    threads = [threading.Thread(target=put), threading.Thread(target=other)]
    for thread in threads:
        thread.start()
    for thread in threads:
        thread.join()

    assert sorted([results["put"]["changed"], results["other"]["changed"]]) == [False, True]
    with Session(database) as session:
        assert session.get(SetupDraftModel, "alice").revision == 2
