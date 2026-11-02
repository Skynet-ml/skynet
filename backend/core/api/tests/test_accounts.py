"""Tests for email/password account registration, sign-in, and hashing.

The endpoint tests run against a shared in-memory SQLite engine (StaticPool so
the table and rows persist across sessions and the TestClient's worker thread)
and set a known ``BACKEND_AUTH_SECRET`` so the internal-auth gate is exercised
rather than bypassed.
"""

from __future__ import annotations

from datetime import UTC, datetime
from typing import Any

import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient
from pydantic import SecretStr
from sqlalchemy import create_engine
from sqlalchemy.orm import Session
from sqlalchemy.pool import StaticPool

from ...brand import APP_NAME
from ...config import settings
from ...storage.models import UserIdentityModel, UserModel
from ..errors import DomainError
from ..password_policy import validate_password
from ..passwords import hash_password, verify_password
from ..routers.accounts import create_accounts_router

_SECRET = "test-internal-secret"
_AUTH_HEADER = {"X-Internal-Auth": _SECRET}


def test_hash_password_round_trips() -> None:
    """A hashed password verifies, a wrong one does not, and salt randomizes."""
    encoded = hash_password("correct horse battery")
    assert verify_password("correct horse battery", encoded) is True
    assert verify_password("wrong password", encoded) is False
    assert encoded != hash_password("correct horse battery")
    assert encoded.startswith("scrypt$")


def test_verify_password_tolerates_malformed_hash() -> None:
    """A garbage stored value verifies as False instead of raising."""
    assert verify_password("anything", "not-a-real-hash") is False
    assert verify_password("anything", "") is False


class _Store:
    """Minimal store exposing only the SQLAlchemy engine the routes need."""

    def __init__(self, engine: Any) -> None:
        """Hold the engine the accounts router opens sessions on.

        Args:
            engine: A SQLAlchemy engine with the ``users`` table created.
        """
        self.engine = engine


@pytest.fixture
def accounts_client(monkeypatch: pytest.MonkeyPatch) -> TestClient:
    """Build a client for the accounts router over in-memory SQLite.

    Args:
        monkeypatch: Pytest fixture used to set the shared internal secret.

    Returns:
        A ``TestClient`` whose app serves register + login against a shared
        in-memory store holding the ``users`` table.
    """
    monkeypatch.setattr(settings, "backend_auth_secret", SecretStr(_SECRET))
    # No SMTP relay here, so registration auto-verifies and sign-in needs no
    # email confirmation; the email-configured path is covered in
    # test_email_verification.py. Pinned so an ambient .env value can't flip it.
    monkeypatch.setattr(settings, "smtp_host", None)
    engine = create_engine("sqlite://", connect_args={"check_same_thread": False}, poolclass=StaticPool)
    UserModel.__table__.create(engine)
    UserIdentityModel.__table__.create(engine)
    store = _Store(engine)
    app = FastAPI()
    app.state.job_store = store
    app.include_router(create_accounts_router(job_store=store))
    return TestClient(app)


def test_register_then_login_succeeds(accounts_client: TestClient) -> None:
    """A registered account can sign in and identity is the lowercased email."""
    created = accounts_client.post(
        "/auth/register",
        json={"email": "Alice@Example.com", "password": "hunter2hunter", "name": "Alice"},
        headers=_AUTH_HEADER,
    )
    assert created.status_code == 201
    assert created.json() == {
        "email": "alice@example.com",
        "name": "Alice",
        "role": "user",
        "first_login": False,
    }

    ok = accounts_client.post(
        "/auth/login",
        json={"email": "alice@example.com", "password": "hunter2hunter"},
        headers=_AUTH_HEADER,
    )
    assert ok.status_code == 200
    assert ok.json()["email"] == "alice@example.com"
    assert ok.json()["first_login"] is True

    again = accounts_client.post(
        "/auth/login",
        json={"email": "alice@example.com", "password": "hunter2hunter"},
        headers=_AUTH_HEADER,
    )
    assert again.status_code == 200
    assert again.json()["first_login"] is False


def test_register_persists_profile_fields(accounts_client: TestClient) -> None:
    """Valid sign-up profile fields are stored; an unknown choice coerces to NULL."""
    created = accounts_client.post(
        "/auth/register",
        json={
            "email": "fran@example.com",
            "password": "longenough1",
            "name": "Fran",
            "use_case": "extraction",
            "experience_level": "expert",
            "job_role": "bogus-role",
        },
        headers=_AUTH_HEADER,
    )
    assert created.status_code == 201
    engine = accounts_client.app.state.job_store.engine
    with Session(engine) as session:
        row = session.get(UserModel, "fran@example.com")
        assert row is not None
        assert row.use_case == "extraction"
        assert row.experience_level == "expert"
        assert row.job_role is None


@pytest.mark.parametrize(
    ("sent", "stored"),
    [
        ("new", "guided"),
        ("familiar", "standard"),
        ("expert", "expert"),
        ("guided", "guided"),
        ("standard", "standard"),
        ("wizard", None),
    ],
)
def test_register_maps_experience_vocabularies(accounts_client: TestClient, sent: str, stored: str | None) -> None:
    """Both the legacy sign-up and the abstraction-level vocabularies store the new values."""
    email = f"level-{sent}@example.com"
    created = accounts_client.post(
        "/auth/register",
        json={"email": email, "password": "longenough1", "experience_level": sent},
        headers=_AUTH_HEADER,
    )
    assert created.status_code == 201
    with Session(accounts_client.app.state.job_store.engine) as session:
        row = session.get(UserModel, email)
        assert row is not None
        assert row.experience_level == stored


@pytest.mark.parametrize(
    ("environment_name", "skipped"), [("staging", False), ("production", False), ("skynet-pr-662", True)]
)
@pytest.mark.parametrize("route", ["/auth/register", "/auth/oauth/provision"])
def test_new_account_skips_intake_only_in_preview_environments(
    accounts_client: TestClient, monkeypatch: pytest.MonkeyPatch, route: str, environment_name: str, skipped: bool
) -> None:
    """A new account starts with the setup intake done only in a PR preview environment."""
    monkeypatch.setattr(settings, "app_environment", environment_name)
    created = accounts_client.post(
        route,
        json={"email": "ivy@example.com", "password": "longenough1", "name": "Ivy"},
        headers=_AUTH_HEADER,
    )
    assert created.status_code in {200, 201}
    with Session(accounts_client.app.state.job_store.engine) as session:
        row = session.get(UserModel, "ivy@example.com")
        assert row is not None
        assert (row.intake_completed_at is not None) is skipped
        assert row.intake_profile is None


def test_register_rejects_duplicate_email(accounts_client: TestClient) -> None:
    """Re-registering an existing email is a 409, regardless of casing."""
    body = {"email": "bob@example.com", "password": "longenough1", "name": "Bob"}
    assert accounts_client.post("/auth/register", json=body, headers=_AUTH_HEADER).status_code == 201
    dupe = accounts_client.post(
        "/auth/register",
        json={"email": "BOB@example.com", "password": "longenough1"},
        headers=_AUTH_HEADER,
    )
    assert dupe.status_code == 409


def test_register_validates_email_and_password(accounts_client: TestClient) -> None:
    """A malformed email or a short password is rejected with 422."""
    bad_email = accounts_client.post(
        "/auth/register",
        json={"email": "not-an-email", "password": "longenough1"},
        headers=_AUTH_HEADER,
    )
    assert bad_email.status_code == 422
    short_pw = accounts_client.post(
        "/auth/register",
        json={"email": "c@example.com", "password": "short"},
        headers=_AUTH_HEADER,
    )
    assert short_pw.status_code == 422


def test_password_policy_rejects_each_rule() -> None:
    """Each acceptance rule fails with its own semantic code."""
    with pytest.raises(DomainError) as short:
        validate_password("short", "alice@example.com")
    assert short.value.code == "accounts.weak_password"
    with pytest.raises(DomainError) as long_pw:
        validate_password("x" * 129, "alice@example.com")
    assert long_pw.value.code == "accounts.password_too_long"
    with pytest.raises(DomainError) as common:
        validate_password("password123", "alice@example.com")
    assert common.value.code == "accounts.password_common"
    with pytest.raises(DomainError) as service:
        validate_password(f"My{APP_NAME}2026", "alice@example.com")
    assert service.value.code == "accounts.password_common"
    with pytest.raises(DomainError) as contains:
        validate_password("Alice-loves-tea", "alice@example.com")
    assert contains.value.code == "accounts.password_contains_email"


def test_password_policy_accepts_strong_passwords() -> None:
    """A passphrase, a 128-char password, and a short local part all pass."""
    validate_password("plum trellis 09 morning", "alice@example.com")
    validate_password("x" * 128, "alice@example.com")
    # A 3-char local part is too generic to count as containing the email.
    validate_password("aliveandwell99", "ali@example.com")


def test_register_rejects_common_password(accounts_client: TestClient) -> None:
    """The blocklist is enforced end-to-end through /auth/register."""
    res = accounts_client.post(
        "/auth/register",
        json={"email": "gia@example.com", "password": "qwertyuiop"},
        headers=_AUTH_HEADER,
    )
    assert res.status_code == 422
    assert res.json()["detail"] == "That password is too common and easy to guess. Pick a different one."


def test_login_rejects_wrong_password_and_unknown_email(accounts_client: TestClient) -> None:
    """Bad password and unknown email both fail with 401."""
    accounts_client.post(
        "/auth/register",
        json={"email": "dora@example.com", "password": "correctpass1"},
        headers=_AUTH_HEADER,
    )
    wrong = accounts_client.post(
        "/auth/login",
        json={"email": "dora@example.com", "password": "wrongpass1"},
        headers=_AUTH_HEADER,
    )
    assert wrong.status_code == 401
    unknown = accounts_client.post(
        "/auth/login",
        json={"email": "ghost@example.com", "password": "whatever12"},
        headers=_AUTH_HEADER,
    )
    assert unknown.status_code == 401


def test_endpoints_require_internal_secret(accounts_client: TestClient) -> None:
    """Calls without the shared internal secret are forbidden."""
    no_secret = accounts_client.post(
        "/auth/register",
        json={"email": "eve@example.com", "password": "longenough1"},
    )
    assert no_secret.status_code == 403
    wrong_secret = accounts_client.post(
        "/auth/login",
        json={"email": "eve@example.com", "password": "longenough1"},
        headers={"X-Internal-Auth": "nope"},
    )
    assert wrong_secret.status_code == 403


def test_login_locks_out_after_repeated_failures(accounts_client: TestClient) -> None:
    """Five wrong passwords lock the email; the sixth attempt is a 429."""
    accounts_client.post(
        "/auth/register",
        json={"email": "hank@example.com", "password": "correctpass1"},
        headers=_AUTH_HEADER,
    )
    for _ in range(5):
        attempt = accounts_client.post(
            "/auth/login",
            json={"email": "hank@example.com", "password": "wrongpass1"},
            headers=_AUTH_HEADER,
        )
        assert attempt.status_code == 401

    locked = accounts_client.post(
        "/auth/login",
        json={"email": "hank@example.com", "password": "wrongpass1"},
        headers=_AUTH_HEADER,
    )
    assert locked.status_code == 429
    assert locked.json()["detail"] == "Too many failed sign-in attempts. Try again later."

    # The lockout holds even once the caller supplies the right password.
    with_right_password = accounts_client.post(
        "/auth/login",
        json={"email": "hank@example.com", "password": "correctpass1"},
        headers=_AUTH_HEADER,
    )
    assert with_right_password.status_code == 429


def test_login_success_resets_failure_counter(accounts_client: TestClient) -> None:
    """A successful sign-in clears earlier failures so the count starts over."""
    accounts_client.post(
        "/auth/register",
        json={"email": "iris@example.com", "password": "correctpass1"},
        headers=_AUTH_HEADER,
    )
    for _ in range(4):
        bad = accounts_client.post(
            "/auth/login",
            json={"email": "iris@example.com", "password": "wrongpass1"},
            headers=_AUTH_HEADER,
        )
        assert bad.status_code == 401

    good = accounts_client.post(
        "/auth/login",
        json={"email": "iris@example.com", "password": "correctpass1"},
        headers=_AUTH_HEADER,
    )
    assert good.status_code == 200

    # With the counter reset, five fresh failures are needed before a lockout;
    # without the reset the second of these would already be a 429.
    for _ in range(5):
        again = accounts_client.post(
            "/auth/login",
            json={"email": "iris@example.com", "password": "wrongpass1"},
            headers=_AUTH_HEADER,
        )
        assert again.status_code == 401


def test_oauth_provision_creates_passwordless_account(accounts_client: TestClient) -> None:
    """A provider identity gets a verified, password-less row and a one-shot first_login."""
    first = accounts_client.post(
        "/auth/oauth/provision",
        json={"email": "Gina@Example.com", "name": "Gina"},
        headers=_AUTH_HEADER,
    )
    assert first.status_code == 200
    assert first.json() == {
        "email": "gina@example.com",
        "name": "Gina",
        "role": "user",
        "first_login": True,
    }
    engine = accounts_client.app.state.job_store.engine
    with Session(engine) as session:
        row = session.get(UserModel, "gina@example.com")
        assert row is not None
        assert row.password_hash == ""
        assert bool(row.email_verified)
        assert row.last_login_at is not None

    again = accounts_client.post(
        "/auth/oauth/provision",
        json={"email": "gina@example.com", "name": "Gina"},
        headers=_AUTH_HEADER,
    )
    assert again.status_code == 200
    assert again.json()["first_login"] is False

    # The row has no password, so the password path can never sign in to it,
    # and the email counts as taken for a fresh sign-up.
    login = accounts_client.post(
        "/auth/login",
        json={"email": "gina@example.com", "password": ""},
        headers=_AUTH_HEADER,
    )
    assert login.status_code == 401
    register = accounts_client.post(
        "/auth/register",
        json={"email": "gina@example.com", "password": "longenough1"},
        headers=_AUTH_HEADER,
    )
    assert register.status_code == 409


def test_oauth_provision_keeps_only_an_https_picture(accounts_client: TestClient) -> None:
    """Each provider sign-in refreshes an https picture; other values leave it as is."""
    engine = accounts_client.app.state.job_store.engine

    def picture() -> str | None:
        """Return the saved picture of the test account."""
        with Session(engine) as session:
            row = session.get(UserModel, "pia@example.com")
            assert row is not None
            return row.image_url

    def sign_in(image: str) -> None:
        """Provision the test account with ``image`` as its provider picture."""
        response = accounts_client.post(
            "/auth/oauth/provision",
            json={"email": "pia@example.com", "name": "Pia", "image": image},
            headers=_AUTH_HEADER,
        )
        assert response.status_code == 200

    sign_in("")
    assert picture() is None
    sign_in("https://lh3.googleusercontent.com/a/pia")
    assert picture() == "https://lh3.googleusercontent.com/a/pia"
    sign_in("javascript:alert(1)")
    sign_in("")
    assert picture() == "https://lh3.googleusercontent.com/a/pia"
    sign_in("https://avatars.githubusercontent.com/u/1")
    assert picture() == "https://avatars.githubusercontent.com/u/1"
    # A picture URL longer than the column must not fail the sign-in.
    sign_in("https://lh3.googleusercontent.com/a/" + "x" * 2100)
    assert picture() == "https://avatars.githubusercontent.com/u/1"


def test_oauth_picture_fills_a_session_picture(accounts_client: TestClient) -> None:
    """A session picture reaches an account that has none, so the Mac app shows it.

    A web session from before ``users.image_url`` existed shows the provider
    picture, but provision never saved it, so hosted whoami had none.
    """
    engine = accounts_client.app.state.job_store.engine
    accounts_client.post(
        "/auth/oauth/provision",
        json={"email": "quinn@example.com", "name": "Quinn"},
        headers=_AUTH_HEADER,
    )

    def picture() -> str | None:
        """Return the saved picture of the test account."""
        with Session(engine) as session:
            row = session.get(UserModel, "quinn@example.com")
            assert row is not None
            return row.image_url

    def sync(image: str) -> int:
        """Send ``image`` as the session picture and return the status code."""
        return accounts_client.post(
            "/auth/oauth/picture",
            json={"email": "Quinn@Example.com", "image": image},
            headers=_AUTH_HEADER,
        ).status_code

    assert picture() is None
    assert sync("https://lh3.googleusercontent.com/a/quinn") == 200
    assert picture() == "https://lh3.googleusercontent.com/a/quinn"
    for unsafe in ("http://example.test/a.png", "javascript:alert(1)", "https://x.test/" + "a" * 2048):
        assert sync(unsafe) == 200
    assert picture() == "https://lh3.googleusercontent.com/a/quinn"


def test_oauth_picture_needs_the_secret_and_an_account(accounts_client: TestClient) -> None:
    """The picture call needs the internal secret and never creates an account."""
    body = {"email": "nobody@example.com", "image": "https://lh3.googleusercontent.com/a/x"}
    assert accounts_client.post("/auth/oauth/picture", json=body).status_code == 403
    assert accounts_client.post("/auth/oauth/picture", json=body, headers=_AUTH_HEADER).status_code == 200
    engine = accounts_client.app.state.job_store.engine
    with Session(engine) as session:
        assert session.get(UserModel, "nobody@example.com") is None


def test_oauth_provision_reuses_local_account(accounts_client: TestClient) -> None:
    """A provider sign-in with a registered email reuses that row, name intact."""
    accounts_client.post(
        "/auth/register",
        json={"email": "hal@example.com", "password": "longenough1", "name": "Hal"},
        headers=_AUTH_HEADER,
    )
    provisioned = accounts_client.post(
        "/auth/oauth/provision",
        json={"email": "HAL@example.com", "name": "Hal From Google"},
        headers=_AUTH_HEADER,
    )
    assert provisioned.status_code == 200
    assert provisioned.json()["name"] == "Hal"
    assert provisioned.json()["first_login"] is True

    # The first sign-in is consumed whichever path it came through.
    login = accounts_client.post(
        "/auth/login",
        json={"email": "hal@example.com", "password": "longenough1"},
        headers=_AUTH_HEADER,
    )
    assert login.status_code == 200
    assert login.json()["first_login"] is False


def test_oauth_provision_requires_secret_and_valid_email(accounts_client: TestClient) -> None:
    """Provisioning is gated by the internal secret and rejects a malformed email."""
    no_secret = accounts_client.post("/auth/oauth/provision", json={"email": "ivy@example.com"})
    assert no_secret.status_code == 403
    bad_email = accounts_client.post("/auth/oauth/provision", json={"email": "not-an-email"}, headers=_AUTH_HEADER)
    assert bad_email.status_code == 422


def test_oauth_provision_links_by_another_verified_email(accounts_client: TestClient) -> None:
    """A provider identity with no account under its main email joins one under another."""
    accounts_client.post("/auth/oauth/provision", json={"email": "jo@gmail.com", "name": "Jo"}, headers=_AUTH_HEADER)
    linked = accounts_client.post(
        "/auth/oauth/provision",
        json={"email": "jo@work.edu", "name": "Jo", "other_emails": ["x@nowhere.com", "JO@gmail.com"]},
        headers=_AUTH_HEADER,
    )
    assert linked.status_code == 200
    assert linked.json()["email"] == "jo@gmail.com"
    assert linked.json()["first_login"] is False
    with Session(accounts_client.app.state.job_store.engine) as session:
        assert session.get(UserModel, "jo@work.edu") is None


def test_oauth_provision_drops_an_unconfirmed_password(accounts_client: TestClient) -> None:
    """A password set on an address nobody confirmed stops working once a provider proves it."""
    engine = accounts_client.app.state.job_store.engine
    with Session(engine) as session:
        session.add(
            UserModel(
                email="kim@example.com",
                name="Kim",
                password_hash=hash_password("attackerpass1"),
                created_at=datetime.now(UTC),
                email_verified=False,
            )
        )
        session.commit()
    provisioned = accounts_client.post("/auth/oauth/provision", json={"email": "kim@example.com"}, headers=_AUTH_HEADER)
    assert provisioned.status_code == 200
    with Session(engine) as session:
        row = session.get(UserModel, "kim@example.com")
        assert row is not None
        assert row.password_hash == ""
        assert bool(row.email_verified)
