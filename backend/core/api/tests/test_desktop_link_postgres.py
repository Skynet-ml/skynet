"""Exercise concurrent desktop link calls against real PostgreSQL row locks (SKY-124, R782B-1 to R782I-1).

SQLite ignores ``FOR UPDATE``, so only PostgreSQL at its default READ
COMMITTED level can show the refresh race. Point ``APP_DESKTOP_LINK_TEST_DB_URL``
at an explicitly disposable database to run these tests.
"""

from __future__ import annotations

import base64
import hashlib
import json
import secrets
import threading
import time
from collections.abc import Callable, Iterator
from datetime import UTC, datetime, timedelta
from types import SimpleNamespace
from typing import Any
from uuid import uuid4

import pytest
from fastapi import FastAPI, Request
from fastapi.responses import JSONResponse
from fastapi.testclient import TestClient
from pydantic import SecretStr
from sqlalchemy import Engine, create_engine, event, text
from sqlalchemy.orm import Session

from core.api import two_factor
from core.api.auth import AuthenticatedUser, get_authenticated_user
from core.api.errors import DomainError
from core.api.passwords import hash_password
from core.api.routers import account_security, accounts, desktop_link
from core.api.routers.account_data import DeleteAccountRequest, create_account_data_router
from core.api.routers.desktop_link import create_desktop_link_router
from core.config import app_env, settings
from core.storage.models import (
    AgentConversationModel,
    ApiTokenModel,
    Base,
    DesktopLinkCodeModel,
    DesktopLinkModel,
    DesktopTokenModel,
    UserIdentityModel,
    UserModel,
    WebAuthnChallengeModel,
    WebAuthnCredentialModel,
)

TEST_DB_URL = app_env("APP_DESKTOP_LINK_TEST_DB_URL")
pytestmark = pytest.mark.skipif(
    not TEST_DB_URL or not TEST_DB_URL.startswith("postgresql"),
    reason="Set APP_DESKTOP_LINK_TEST_DB_URL to an explicitly disposable PostgreSQL database.",
)

REDIRECT = "http://127.0.0.1:54321/local/hosted-link/callback"
MAC_HOLD_SECONDS = 0.5
DELETION_HOLD_SECONDS = 0.5
ALICE = "alice@example.test"
ALICE_PASSWORD = "Correct-horse-9"
INTERNAL_SECRET = "internal-test-secret"
# Each statement locks one row that the account deletion deletes. A blocker
# transaction that holds it parks the deletion at that point (R782F-1).
DELETION_STOPS = {
    "agent-conversations": "SELECT 1 FROM agent_conversations WHERE username = :u FOR UPDATE",
    "user-identities": "SELECT 1 FROM user_identities WHERE username = :u FOR UPDATE",
    "api-tokens": "SELECT 1 FROM api_tokens WHERE username = :u FOR UPDATE",
    "passkey-challenges": "SELECT 1 FROM webauthn_challenges WHERE user_email = :u FOR UPDATE",
    "desktop-tokens": "SELECT 1 FROM desktop_tokens FOR UPDATE",
}
SIGN_IN_STOPS = ["agent-conversations", "passkey-challenges", "user-identities", "api-tokens"]


class _Store:
    """Minimal store exposing the engine and the MAU admission hook."""

    def __init__(self, engine: Engine) -> None:
        """Hold the engine the routes open sessions on.

        Args:
            engine: Engine bound to the test schema.
        """
        self.engine = engine

    def admit_monthly_active_user(self, username: str, month_start: Any, cap: int) -> bool:
        """Admit every identity; the MAU cap is not under test here.

        Args:
            username: Identity to admit.
            month_start: First day of the month.
            cap: Configured cap.

        Returns:
            Always True.
        """
        return True


async def _domain_error_json(request: Request, exc: Exception) -> JSONResponse:
    """Render a DomainError with its semantic code, like the app's handler.

    Args:
        request: Incoming request.
        exc: The raised DomainError.

    Returns:
        A JSON body with ``code``.
    """
    assert isinstance(exc, DomainError)
    return JSONResponse(status_code=exc.status_code, content={"code": exc.code})


def _add_alice(engine: Engine) -> None:
    """Create the account row of the signed-in browser user.

    Args:
        engine: Engine bound to the test schema.
    """
    with Session(engine) as session:
        session.add(
            UserModel(email="alice@example.test", name="Alice Example", password_hash=hash_password(ALICE_PASSWORD))
        )
        session.commit()


def _delete_alice(engine: Engine) -> None:
    """Delete alice through the served account-deletion route, with all its locks (R782H-1).

    The route function runs in the calling thread, so the tests' per-thread
    statement holds still apply.

    Args:
        engine: Engine bound to the test schema.
    """
    router = create_account_data_router(job_store=_Store(engine))
    endpoint = next(route.endpoint for route in router.routes if route.path == "/account/delete")
    endpoint(
        body=DeleteAccountRequest(password=ALICE_PASSWORD),
        user=AuthenticatedUser(username=ALICE, role="user", groups=()),
    )


@pytest.fixture
def database() -> Iterator[Engine]:
    """Create the desktop link tables in a fresh schema and drop it after.

    Returns:
        An engine whose search path is the new schema.
    """
    schema = f"desktop_link_test_{uuid4().hex}"
    admin = create_engine(TEST_DB_URL)
    with admin.begin() as connection:
        connection.execute(text(f'CREATE SCHEMA "{schema}"'))
    engine = create_engine(TEST_DB_URL, connect_args={"options": f"-csearch_path={schema}"})
    tables = [model.__table__ for model in (DesktopLinkModel, DesktopTokenModel, DesktopLinkCodeModel, UserModel)]
    try:
        Base.metadata.create_all(engine, tables=tables)
        _add_alice(engine)
        yield engine
    finally:
        engine.dispose()
        with admin.begin() as connection:
            connection.execute(text(f'DROP SCHEMA "{schema}" CASCADE'))
        admin.dispose()


@pytest.fixture
def full_database() -> Iterator[Engine]:
    """Create every table in a fresh schema and drop it after.

    Account deletion touches every owned table, so it needs the whole schema.
    The pgvector types resolve from ``public``, where the extension lives.

    Returns:
        An engine whose search path is the new schema, then ``public``.
    """
    schema = f"desktop_link_test_{uuid4().hex}"
    admin = create_engine(TEST_DB_URL)
    with admin.begin() as connection:
        connection.execute(text("CREATE EXTENSION IF NOT EXISTS vector"))
        connection.execute(text(f'CREATE SCHEMA "{schema}"'))
    engine = create_engine(TEST_DB_URL, connect_args={"options": f"-csearch_path={schema},public"})
    try:
        Base.metadata.create_all(engine)
        _add_alice(engine)
        yield engine
    finally:
        engine.dispose()
        with admin.begin() as connection:
            connection.execute(text(f'DROP SCHEMA "{schema}" CASCADE'))
        admin.dispose()


def _client(engine: Engine) -> TestClient:
    """Build an app with the desktop link router over ``engine``.

    Args:
        engine: Engine bound to the test schema.

    Returns:
        A test client acting as a signed-in browser user.
    """
    store = _Store(engine)
    app = FastAPI()
    app.state.job_store = store
    app.add_exception_handler(DomainError, _domain_error_json)
    app.include_router(create_desktop_link_router(job_store=store))
    app.dependency_overrides[get_authenticated_user] = lambda: AuthenticatedUser(
        username="alice@example.test", role="user", groups=()
    )
    return TestClient(app)


def _approve(client: TestClient) -> tuple[str, str]:
    """Approve a Mac link in the browser.

    Args:
        client: Test client.

    Returns:
        The link code and its PKCE verifier.
    """
    verifier = secrets.token_urlsafe(48)
    challenge = base64.urlsafe_b64encode(hashlib.sha256(verifier.encode()).digest()).decode().rstrip("=")
    code = client.post(
        "/desktop/link/authorize",
        json={
            "redirect_uri": REDIRECT,
            "code_challenge": challenge,
            "code_challenge_method": "S256",
            "scope": "identity",
            "device_name": "Mac",
        },
    ).json()["code"]
    return code, verifier


def _exchange(client: TestClient, code: str, verifier: str) -> Any:
    """Exchange a link code at the token endpoint.

    Args:
        client: Test client.
        code: Link code.
        verifier: PKCE verifier.

    Returns:
        The token endpoint response.
    """
    return client.post(
        "/desktop/link/token",
        json={
            "grant_type": "authorization_code",
            "code": code,
            "code_verifier": verifier,
            "redirect_uri": REDIRECT,
            "client_id": "skynet-desktop",
        },
    )


def _link_pair(client: TestClient) -> dict[str, Any]:
    """Authorize a Mac and exchange its code for the first token pair.

    Args:
        client: Test client.

    Returns:
        The token endpoint JSON body.
    """
    return _exchange(client, *_approve(client)).json()


def _refresh(client: TestClient, refresh_token: str) -> Any:
    """Call the refresh grant.

    Args:
        client: Test client.
        refresh_token: Refresh token to rotate.

    Returns:
        The token endpoint response.
    """
    return client.post("/desktop/link/token", json={"grant_type": "refresh_token", "refresh_token": refresh_token})


def test_replayed_token_during_a_concurrent_rotation_revokes_the_link(
    database: Engine, monkeypatch: pytest.MonkeyPatch
) -> None:
    """A thief replaying RT0 while the Mac rotates RT1 cannot fork the link (R782B-1).

    The Mac's refresh holds its transaction open inside the rate-limit call.
    The thief starts during that hold. Without the link lock, the thief reads
    no used successor, waits on the RT1 row, and both calls get a pair.
    """
    first_call = threading.Lock()
    mac_in_transaction = threading.Event()

    def hold_first_refresh(key: str, *, limit: int, window_seconds: int) -> None:
        """Hold the first refresh transaction open; let later calls pass.

        Args:
            key: Limiter key.
            limit: Unused.
            window_seconds: Unused.
        """
        if key.startswith("desktop-link-refresh") and first_call.acquire(blocking=False):
            mac_in_transaction.set()
            time.sleep(MAC_HOLD_SECONDS)

    client = _client(database)
    verifier = secrets.token_urlsafe(48)
    challenge = base64.urlsafe_b64encode(hashlib.sha256(verifier.encode()).digest()).decode().rstrip("=")
    code = client.post(
        "/desktop/link/authorize",
        json={
            "redirect_uri": REDIRECT,
            "code_challenge": challenge,
            "code_challenge_method": "S256",
            "scope": "identity",
            "device_name": "Mac",
        },
    ).json()["code"]
    pair = client.post(
        "/desktop/link/token",
        json={
            "grant_type": "authorization_code",
            "code": code,
            "code_verifier": verifier,
            "redirect_uri": REDIRECT,
            "client_id": "skynet-desktop",
        },
    ).json()
    rt0 = pair["refresh_token"]
    rt1 = _refresh(client, rt0).json()["refresh_token"]

    monkeypatch.setattr(desktop_link, "_limit", hold_first_refresh)
    monkeypatch.setattr(desktop_link, "_send_notice", lambda *args: None)
    results: dict[str, Any] = {}

    def mac() -> None:
        """Rotate the Mac's current refresh token."""
        results["mac"] = _refresh(client, rt1)

    def thief() -> None:
        """Replay the old refresh token once the Mac is mid-rotation."""
        if not mac_in_transaction.wait(timeout=5):
            results["thief"] = "no-hold"
            return
        results["thief"] = _refresh(client, rt0)

    threads = [threading.Thread(target=mac), threading.Thread(target=thief)]
    for thread in threads:
        thread.start()
    for thread in threads:
        thread.join(timeout=10)

    assert not isinstance(results["thief"], str), results["thief"]
    assert results["mac"].status_code == 200
    assert results["thief"].status_code == 401
    assert results["thief"].json()["code"] == "desktop.link_revoked"
    with Session(database) as session:
        assert session.get(DesktopLinkModel, pair["link_id"]).revoked_at is not None
    assert _refresh(client, results["mac"].json()["refresh_token"]).status_code == 401


def test_account_deletion_during_a_refresh_does_not_deadlock(
    full_database: Engine, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Account deletion and a Mac refresh lock link rows and token rows in one order (R782C-1).

    The deletion holds its transaction open after it deletes the links. The refresh starts during that hold. Refresh locks the link,
    then the token. When deletion locked the tokens first, the two
    transactions waited on each other and PostgreSQL aborted one of them.
    """
    monkeypatch.setattr(desktop_link, "_limit", lambda *args, **kwargs: None)
    monkeypatch.setattr(desktop_link, "_send_notice", lambda *args: None)
    client = _client(full_database)
    pair = _link_pair(client)

    deletion_thread_name = "account-deletion"
    deletion_holds_lock = threading.Event()

    def hold_after_link_delete(conn: Any, cursor: Any, statement: str, *args: Any) -> None:
        """Hold the deletion transaction open after it deletes the links.

        Args:
            conn: Connection that ran the statement.
            cursor: DBAPI cursor.
            statement: SQL text.
            args: Unused event arguments.
        """
        if (
            threading.current_thread().name == deletion_thread_name
            and not deletion_holds_lock.is_set()
            and statement.lstrip().upper().startswith("DELETE FROM DESKTOP_LINKS")
        ):
            deletion_holds_lock.set()
            time.sleep(DELETION_HOLD_SECONDS)

    event.listen(full_database, "after_cursor_execute", hold_after_link_delete)
    results: dict[str, Any] = {}

    def deletion() -> None:
        """Delete the account in one transaction, like the account router."""
        try:
            _delete_alice(full_database)
            results["deletion"] = "ok"
        except Exception as exc:  # The deadlock error is the observed result.
            results["deletion"] = type(exc).__name__

    def mac() -> None:
        """Refresh the Mac's pair while the deletion holds its first lock."""
        if not deletion_holds_lock.wait(timeout=5):
            results["mac"] = "no-hold"
            return
        try:
            results["mac"] = _refresh(client, pair["refresh_token"]).status_code
        except Exception as exc:  # The deadlock error is the observed result.
            results["mac"] = type(exc).__name__

    threads = [threading.Thread(target=deletion, name=deletion_thread_name), threading.Thread(target=mac)]
    try:
        for thread in threads:
            thread.start()
        for thread in threads:
            thread.join(timeout=15)
    finally:
        event.remove(full_database, "after_cursor_execute", hold_after_link_delete)

    assert results == {"deletion": "ok", "mac": 401}
    with Session(full_database) as session:
        assert session.query(DesktopLinkModel).count() == 0
        assert session.query(DesktopTokenModel).count() == 0


@pytest.mark.parametrize("pause", ["after-link-delete", "after-token-delete", "during-exchange"])
def test_code_exchange_during_account_deletion_leaves_no_link(
    full_database: Engine, monkeypatch: pytest.MonkeyPatch, pause: str
) -> None:
    """A Mac that exchanges its code while the account is deleted gets no live link (R782D-1).

    The owner has one linked Mac and approves a second one, then deletes the
    account before the second Mac exchanges its code. The first two cases hold the deletion open after its
    link or token statement and exchange during the hold. The third case holds
    the exchange open after it locks the code and deletes during that hold.
    When the deletion read the links before the exchange committed its new
    link, that link and its tokens outlived the account.
    """
    monkeypatch.setattr(desktop_link, "_limit", lambda *args, **kwargs: None)
    monkeypatch.setattr(desktop_link, "_send_notice", lambda *args: None)
    client = _client(full_database)
    _link_pair(client)
    code, verifier = _approve(client)

    deletion_thread_name = "account-deletion"
    other_side_holds = threading.Event()
    held_statement = "DELETE FROM DESKTOP_LINKS" if pause == "after-link-delete" else "DELETE FROM DESKTOP_TOKENS"

    def hold_deletion(conn: Any, cursor: Any, statement: str, *args: Any) -> None:
        """Hold the deletion transaction open after the chosen statement.

        Args:
            conn: Connection that ran the statement.
            cursor: DBAPI cursor.
            statement: SQL text.
            args: Unused event arguments.
        """
        if (
            threading.current_thread().name == deletion_thread_name
            and not other_side_holds.is_set()
            and statement.lstrip().upper().startswith(held_statement)
        ):
            other_side_holds.set()
            time.sleep(DELETION_HOLD_SECONDS)

    live_link_count = desktop_link._live_link_count

    def hold_exchange(session: Session, username: str, now: Any) -> int:
        """Hold the first exchange transaction open while it locks the code.

        Args:
            session: Exchange session.
            username: Code owner.
            now: Current UTC time.

        Returns:
            The real live link count.
        """
        if not other_side_holds.is_set():
            other_side_holds.set()
            time.sleep(MAC_HOLD_SECONDS)
        return live_link_count(session, username, now)

    if pause == "during-exchange":
        monkeypatch.setattr(desktop_link, "_live_link_count", hold_exchange)
    else:
        event.listen(full_database, "after_cursor_execute", hold_deletion)
    results: dict[str, Any] = {}

    def deletion() -> None:
        """Delete the account in one transaction, like the account router."""
        if pause == "during-exchange" and not other_side_holds.wait(timeout=5):
            results["deletion"] = "no-hold"
            return
        try:
            _delete_alice(full_database)
            results["deletion"] = "ok"
        except Exception as exc:  # A lock error is the observed result.
            results["deletion"] = type(exc).__name__

    def mac() -> None:
        """Exchange the code; wait for the deletion hold first when it leads."""
        if pause != "during-exchange" and not other_side_holds.wait(timeout=5):
            results["mac"] = "no-hold"
            return
        results["mac"] = _exchange(client, code, verifier)

    threads = [
        threading.Thread(target=deletion, name=deletion_thread_name),
        threading.Thread(target=mac, name="mac"),
    ]
    try:
        for thread in threads:
            thread.start()
        for thread in threads:
            thread.join(timeout=15)
    finally:
        if pause != "during-exchange":
            event.remove(full_database, "after_cursor_execute", hold_deletion)

    assert results["deletion"] == "ok"
    exchange = results["mac"]
    assert not isinstance(exchange, str), exchange
    with Session(full_database) as session:
        assert session.query(DesktopLinkModel).count() == 0
        assert session.query(DesktopTokenModel).count() == 0
        assert session.query(DesktopLinkCodeModel).count() == 0
    if pause == "during-exchange":
        # The exchange commits first; the deletion then removes what it made.
        assert exchange.status_code == 200
        whoami = client.get(
            "/desktop/v1/whoami", headers={"Authorization": f"Bearer {exchange.json()['access_token']}"}
        )
        assert whoami.status_code == 401
    else:
        # The deletion removed the code first, so the exchange finds none.
        assert exchange.status_code == 400


def _authorize(client: TestClient) -> tuple[Any, str]:
    """Ask the browser to approve a Mac link.

    Args:
        client: Test client.

    Returns:
        The authorize response and the PKCE verifier for its code.
    """
    verifier = secrets.token_urlsafe(48)
    challenge = base64.urlsafe_b64encode(hashlib.sha256(verifier.encode()).digest()).decode().rstrip("=")
    response = client.post(
        "/desktop/link/authorize",
        json={
            "redirect_uri": REDIRECT,
            "code_challenge": challenge,
            "code_challenge_method": "S256",
            "scope": "identity",
            "device_name": "Mac",
        },
    )
    return response, verifier


@pytest.mark.parametrize("code_kind", ["fresh", "pre-approved"])
@pytest.mark.parametrize(
    "held_statement",
    ["DELETE FROM DESKTOP_LINK_CODES", "DELETE FROM DESKTOP_LINKS", "DELETE FROM DESKTOP_TOKENS", "DELETE FROM USERS"],
)
def test_link_approval_during_account_deletion_leaves_no_link(
    full_database: Engine, monkeypatch: pytest.MonkeyPatch, held_statement: str, code_kind: str
) -> None:
    """A Mac linked while the account deletion is open gets no live link (R782E-1).

    The deletion holds its transaction open after the chosen statement. During
    the hold, the browser approves a Mac (``fresh``) or a Mac exchanges a code
    approved before the deletion (``pre-approved``). When authorize and the
    exchange read the user row without a lock, they saw the uncommitted
    deletion's user row, so a code made after the code delete became a link
    that outlived the account.
    """
    monkeypatch.setattr(desktop_link, "_limit", lambda *args, **kwargs: None)
    monkeypatch.setattr(desktop_link, "_send_notice", lambda *args: None)
    client = _client(full_database)
    _link_pair(client)
    approved_code, approved_verifier = _approve(client) if code_kind == "pre-approved" else ("", "")

    deletion_thread_name = "account-deletion"
    deletion_holds = threading.Event()

    def hold_deletion(conn: Any, cursor: Any, statement: str, *args: Any) -> None:
        """Hold the deletion transaction open after the chosen statement.

        Args:
            conn: Connection that ran the statement.
            cursor: DBAPI cursor.
            statement: SQL text.
            args: Unused event arguments.
        """
        if (
            threading.current_thread().name == deletion_thread_name
            and not deletion_holds.is_set()
            and statement.lstrip().upper().startswith(held_statement)
        ):
            deletion_holds.set()
            time.sleep(DELETION_HOLD_SECONDS)

    event.listen(full_database, "after_cursor_execute", hold_deletion)
    results: dict[str, Any] = {}

    def deletion() -> None:
        """Delete the account in one transaction, like the account router."""
        try:
            _delete_alice(full_database)
            results["deletion"] = "ok"
        except Exception as exc:  # A lock error is the observed result.
            results["deletion"] = type(exc).__name__

    def mac() -> None:
        """Approve and exchange, or exchange the earlier code, during the hold."""
        if not deletion_holds.wait(timeout=5):
            results["mac"] = "no-hold"
            return
        code, verifier = approved_code, approved_verifier
        if code_kind == "fresh":
            authorize, verifier = _authorize(client)
            results["authorize"] = authorize.status_code
            if authorize.status_code != 200:
                return
            code = authorize.json()["code"]
        results["exchange"] = _exchange(client, code, verifier)

    threads = [
        threading.Thread(target=deletion, name=deletion_thread_name),
        threading.Thread(target=mac, name="mac"),
    ]
    try:
        for thread in threads:
            thread.start()
        for thread in threads:
            thread.join(timeout=15)
    finally:
        event.remove(full_database, "after_cursor_execute", hold_deletion)

    assert results["deletion"] == "ok"
    assert results.get("mac") != "no-hold"
    exchange = results.get("exchange")
    if exchange is not None and exchange.status_code == 200:
        whoami = client.get(
            "/desktop/v1/whoami", headers={"Authorization": f"Bearer {exchange.json()['access_token']}"}
        )
        assert whoami.status_code == 401
    with Session(full_database) as session:
        assert session.query(UserModel).count() == 0
        assert session.query(DesktopLinkModel).count() == 0
        assert session.query(DesktopTokenModel).count() == 0
        assert session.query(DesktopLinkCodeModel).count() == 0
    if code_kind == "fresh":
        # Authorize waits for the deletion, then finds no account.
        assert results["authorize"] == 401
    else:
        assert exchange.status_code in (400, 401)


def test_account_deletion_during_an_authorize_removes_the_new_code(
    full_database: Engine, monkeypatch: pytest.MonkeyPatch
) -> None:
    """A deletion that starts inside an open authorize removes that code (R782E-1).

    Authorize holds its transaction open after it locks the user row. The
    deletion starts during that hold and must wait, so its code delete sees
    the code that authorize commits.
    """
    monkeypatch.setattr(desktop_link, "_limit", lambda *args, **kwargs: None)
    monkeypatch.setattr(desktop_link, "_send_notice", lambda *args: None)
    client = _client(full_database)
    authorize_holds = threading.Event()
    live_link_count = desktop_link._live_link_count

    def hold_authorize(session: Session, username: str, now: Any) -> int:
        """Hold the authorize transaction open after its account check.

        Args:
            session: Authorize session.
            username: Signed-in user.
            now: Current UTC time.

        Returns:
            The real live link count.
        """
        if not authorize_holds.is_set():
            authorize_holds.set()
            time.sleep(MAC_HOLD_SECONDS)
        return live_link_count(session, username, now)

    monkeypatch.setattr(desktop_link, "_live_link_count", hold_authorize)
    results: dict[str, Any] = {}

    def browser() -> None:
        """Approve a Mac link."""
        authorize, verifier = _authorize(client)
        results["authorize"] = authorize
        results["verifier"] = verifier

    def deletion() -> None:
        """Delete the account once authorize holds its transaction open."""
        if not authorize_holds.wait(timeout=5):
            results["deletion"] = "no-hold"
            return
        _delete_alice(full_database)
        results["deletion"] = "ok"

    threads = [threading.Thread(target=browser), threading.Thread(target=deletion)]
    for thread in threads:
        thread.start()
    for thread in threads:
        thread.join(timeout=15)

    assert results["deletion"] == "ok"
    assert results["authorize"].status_code == 200
    with Session(full_database) as session:
        assert session.query(DesktopLinkCodeModel).count() == 0
    exchange = _exchange(client, results["authorize"].json()["code"], results["verifier"])
    assert exchange.status_code == 400


def _lock_waiters(engine: Engine) -> int:
    """Count the sessions of this database that wait for a row lock.

    Args:
        engine: Engine bound to the test database.

    Returns:
        The number of waiting sessions.
    """
    with engine.connect() as connection:
        return int(
            connection.execute(
                text(
                    "SELECT count(*) FROM pg_stat_activity"
                    " WHERE datname = current_database() AND wait_event_type = 'Lock'"
                )
            ).scalar_one()
        )


def _wait_for_lock_waiters(engine: Engine, count: int, timeout: float) -> bool:
    """Wait until ``count`` sessions wait for a row lock.

    Args:
        engine: Engine bound to the test database.
        count: Number of waiting sessions to wait for.
        timeout: Seconds to wait.

    Returns:
        True when that many sessions wait before the timeout.
    """
    end = time.monotonic() + timeout
    while time.monotonic() < end:
        if _lock_waiters(engine) >= count:
            return True
        time.sleep(0.05)
    return False


def _run_during_parked_deletion(engine: Engine, stop: str, call: Callable[[], Any]) -> dict[str, Any]:
    """Run ``call`` while a blocker parks the account deletion at ``stop``.

    The blocker holds one row the deletion deletes. When the deletion and
    ``call`` both wait, the blocker commits and both continue.

    Args:
        engine: Engine bound to the test schema.
        stop: Key of :data:`DELETION_STOPS`.
        call: The concurrent request; returns a response.

    Returns:
        ``deletion`` and ``call`` outcomes: "ok" or a status code, or the
        exception class name.
    """
    results: dict[str, Any] = {}
    blocker = engine.connect()
    blocker_transaction = blocker.begin()
    blocker.execute(text(DELETION_STOPS[stop]), {"u": ALICE})

    def deletion() -> None:
        """Delete the account in one transaction, like the account router."""
        try:
            _delete_alice(engine)
            results["deletion"] = "ok"
        except Exception as exc:  # A deadlock error is the observed result.
            results["deletion"] = type(exc).__name__

    def request() -> None:
        """Send the concurrent request."""
        try:
            response = call()
            results["call"] = response.status_code
            results["body"] = response.json()
        except Exception as exc:  # A deadlock or stale-row error is the observed result.
            results["call"] = type(exc).__name__
            results["detail"] = repr(exc)[:300]

    deletion_thread = threading.Thread(target=deletion)
    request_thread = threading.Thread(target=request)
    try:
        deletion_thread.start()
        assert _wait_for_lock_waiters(engine, 1, timeout=5), "the deletion did not reach the blocked row"
        request_thread.start()
        _wait_for_lock_waiters(engine, 2, timeout=2)
        request_thread.join(timeout=0.5)
    finally:
        blocker_transaction.commit()
        blocker.close()
        deletion_thread.join(timeout=20)
        request_thread.join(timeout=20)
    return results


@pytest.fixture
def sign_in(full_database: Engine, monkeypatch: pytest.MonkeyPatch) -> SimpleNamespace:
    """Give alice a password, email 2FA, a passkey, and rows at each deletion stop.

    Only the passkey signature check is replaced: a test cannot produce a
    signed assertion. The routes, their SQL, and the deletion are real.

    Returns:
        A client for the sign-in routes and alice's emailed sign-in code.
    """
    now = datetime.now(UTC)
    with Session(full_database) as session:
        alice = session.get(UserModel, ALICE)
        assert alice is not None
        alice.password_hash = hash_password(ALICE_PASSWORD)
        alice.email_verified = True
        alice.email_2fa_enabled = True
        session.add(ApiTokenModel(username=ALICE, token_hash="h" * 64, last4="abcd"))
        session.add(AgentConversationModel(id=str(uuid4()), username=ALICE, title="Notes"))
        session.add(UserIdentityModel(provider="github", provider_account_id="4242", username=ALICE))
        session.add(
            WebAuthnChallengeModel(
                challenge="registerchallenge1",
                purpose="register",
                user_email=ALICE,
                expires_at=now + timedelta(minutes=5),
            )
        )
        session.add(
            WebAuthnChallengeModel(
                challenge="signinchallenge1", purpose="auth", user_email=None, expires_at=now + timedelta(minutes=5)
            )
        )
        session.add(
            WebAuthnCredentialModel(
                credential_id=_b64url(b"mac-passkey1"),
                user_email=ALICE,
                public_key=_b64url(b"public-key12"),
                sign_count=0,
                nickname="Mac",
            )
        )
        session.commit()
        email_code = two_factor.issue_email_code(session, ALICE)
        session.commit()
    monkeypatch.setattr(settings, "backend_auth_secret", SecretStr(INTERNAL_SECRET))
    monkeypatch.setattr(settings, "max_monthly_active_users", 0)
    monkeypatch.setattr(two_factor, "email_configured", lambda: True)
    monkeypatch.setattr(accounts, "email_configured", lambda: True)
    monkeypatch.setattr(
        account_security, "verify_authentication_response", lambda **kwargs: SimpleNamespace(new_sign_count=7)
    )
    store = _Store(full_database)
    app = FastAPI()
    app.add_exception_handler(DomainError, _domain_error_json)
    app.include_router(accounts.create_accounts_router(job_store=store))
    app.include_router(account_security.create_account_security_router(job_store=store))
    return SimpleNamespace(client=TestClient(app), email_code=email_code)


def _b64url(raw: bytes) -> str:
    """Encode bytes as unpadded base64url, like WebAuthn.

    Args:
        raw: Bytes to encode.

    Returns:
        The encoded text.
    """
    return base64.urlsafe_b64encode(raw).decode().rstrip("=")


def _assert_account_gone(engine: Engine) -> None:
    """Check that the deletion removed alice and her sign-in rows.

    Args:
        engine: Engine bound to the test schema.
    """
    with Session(engine) as session:
        assert session.query(UserModel).count() == 0
        assert session.query(WebAuthnCredentialModel).count() == 0


@pytest.mark.parametrize("stop", SIGN_IN_STOPS)
def test_passkey_sign_in_during_account_deletion_does_not_deadlock(
    full_database: Engine, sign_in: SimpleNamespace, stop: str
) -> None:
    """Passkey sign-in and account deletion lock their rows in one order (R782F-1).

    Passkey sign-in writes the passkey row, then the user row. When the
    deletion locked the user row first and deleted passkeys later, the two
    transactions waited on each other and PostgreSQL aborted one of them.
    """
    client_data = _b64url(json.dumps({"challenge": "signinchallenge1"}).encode())
    credential = {
        "id": _b64url(b"mac-passkey1"),
        "rawId": _b64url(b"mac-passkey1"),
        "response": {"clientDataJSON": client_data},
    }

    def passkey_sign_in() -> Any:
        """Finish a passkey sign-in."""
        return sign_in.client.post(
            "/auth/webauthn/verify", json={"credential": credential}, headers={"X-Internal-Auth": INTERNAL_SECRET}
        )

    results = _run_during_parked_deletion(full_database, stop, passkey_sign_in)

    assert results["deletion"] == "ok", results
    # A sign-in that commits first succeeds. A sign-in that waits for the
    # deletion finds its passkey row gone (StaleDataError, as on staging).
    assert results["call"] in (200, "StaleDataError"), results
    _assert_account_gone(full_database)


@pytest.mark.parametrize("stop", SIGN_IN_STOPS)
def test_email_code_sign_in_during_account_deletion_does_not_deadlock(
    full_database: Engine, sign_in: SimpleNamespace, stop: str
) -> None:
    """Email-code sign-in and account deletion lock their rows in one order (R782F-1).

    Password sign-in with an emailed code writes the user row, then deletes
    the code. The deletion must lock the user row before it deletes codes, or
    the two transactions wait on each other.
    """

    def email_code_sign_in() -> Any:
        """Sign in with the password and the emailed code."""
        return sign_in.client.post(
            "/auth/login",
            json={"email": ALICE, "password": ALICE_PASSWORD, "email_code": sign_in.email_code},
            headers={"X-Internal-Auth": INTERNAL_SECRET},
        )

    results = _run_during_parked_deletion(full_database, stop, email_code_sign_in)

    assert results["deletion"] == "ok", results
    # A sign-in that commits first succeeds; one that waits finds no account.
    assert results["call"] in (200, 401, "StaleDataError"), results
    _assert_account_gone(full_database)


@pytest.mark.parametrize("stop", SIGN_IN_STOPS)
def test_first_oauth_sign_in_during_account_deletion_leaves_no_identity(
    full_database: Engine, sign_in: SimpleNamespace, stop: str
) -> None:
    """A first Google sign-in during account deletion leaves no identity row (R782I-1).

    The sign-in adds an identity row and writes the user row in one
    transaction. When the deletion deleted identities before its user lock,
    a sign-in that ran after that delete committed a new identity row that
    the deletion never saw. The row outlived the account and would sign in
    to a later account registered under the same email.
    """

    def first_oauth_sign_in() -> Any:
        """Sign in with a Google account that is not linked yet."""
        return sign_in.client.post(
            "/auth/oauth/provision",
            json={"email": ALICE, "name": "Alice", "provider": "google", "provider_account_id": "g-999"},
            headers={"X-Internal-Auth": INTERNAL_SECRET},
        )

    results = _run_during_parked_deletion(full_database, stop, first_oauth_sign_in)

    assert results["deletion"] == "ok", results
    # A sign-in that commits first succeeds; one that waits for the user lock
    # finds the user row gone.
    assert results["call"] in (200, "StaleDataError"), results
    _assert_account_gone(full_database)
    with Session(full_database) as session:
        assert session.query(UserIdentityModel).count() == 0


@pytest.mark.parametrize("stop", DELETION_STOPS)
def test_oauth_link_during_account_deletion_leaves_no_identity(
    full_database: Engine, sign_in: SimpleNamespace, stop: str
) -> None:
    """Linking a provider during account deletion cannot leave an orphan identity."""
    with Session(full_database) as session:
        expires = datetime.now(UTC) + timedelta(days=1)
        session.add(
            DesktopLinkModel(id="link", username=ALICE, device_name="Mac", scopes="", absolute_expires_at=expires)
        )
        session.add(DesktopTokenModel(token_hash="token", link_id="link", kind="access", expires_at=expires))
        session.commit()

    def link_oauth() -> Any:
        """Link a Google account to Alice."""
        return sign_in.client.post(
            "/auth/oauth/link",
            json={"username": ALICE, "provider": "google", "provider_account_id": "g-999"},
            headers={"X-Internal-Auth": INTERNAL_SECRET},
        )

    results = _run_during_parked_deletion(full_database, stop, link_oauth)

    assert results["deletion"] == "ok", results
    _assert_account_gone(full_database)
    with Session(full_database) as session:
        assert session.query(UserIdentityModel).count() == 0
    assert results["call"] in (200, 404), results
    if results["call"] == 404:
        assert results["body"] == {"code": "accounts.not_found"}


def test_desktop_call_during_account_deletion_reports_a_revoked_link(
    full_database: Engine, monkeypatch: pytest.MonkeyPatch
) -> None:
    """A desktop call that waits on a deleted link gets 401, not 500 (R782F-2).

    The deletion parks after it deletes the links. The call reads the link
    before that delete commits, then waits to record its use. After the
    deletion commits, the update matches no row.
    """
    monkeypatch.setattr(desktop_link, "_limit", lambda *args, **kwargs: None)
    monkeypatch.setattr(desktop_link, "_send_notice", lambda *args: None)
    client = _client(full_database)
    pair = _link_pair(client)

    def whoami() -> Any:
        """Call a desktop route with the Mac's access token."""
        return client.get("/desktop/v1/whoami", headers={"Authorization": f"Bearer {pair['access_token']}"})

    results = _run_during_parked_deletion(full_database, "desktop-tokens", whoami)

    assert results["deletion"] == "ok", results
    assert results["call"] == 401, results
    assert results["body"] == {"code": "desktop.link_revoked"}
    assert whoami().status_code == 401
