"""Tests for the local ``/local/hosted-link/*`` routes against a fake hosted API."""

from __future__ import annotations

import html
import subprocess
from types import SimpleNamespace
from urllib.parse import parse_qs, urlsplit

import pytest
from cryptography.fernet import Fernet
from fastapi import FastAPI
from fastapi.responses import JSONResponse
from fastapi.testclient import TestClient
from pydantic import SecretStr
from sqlalchemy import create_engine
from sqlalchemy.engine import Engine
from sqlalchemy.pool import StaticPool

from ...brand import APP_NAME
from ...config import settings
from ...connectors import oauth
from ...i18n_en import t_en
from ...local import hosted_link
from ...local.tests.fake_hosted import API_ORIGIN, APP_ORIGIN, FakeHosted
from ...storage.models import UserConnectorModel
from ..auth import AuthenticatedUser, get_authenticated_user
from ..errors import DomainError
from ..routers import local_hosted_link
from ..routers.local_hosted_link import create_local_hosted_link_router

_USER = "local@skynet.local"


@pytest.fixture
def hosted(monkeypatch: pytest.MonkeyPatch) -> FakeHosted:
    """Turn on local mode and point the link client at a fake hosted API.

    Args:
        monkeypatch: Pytest fixture used to change settings and the transport.

    Returns:
        The fake server.
    """
    fake = FakeHosted()
    monkeypatch.setattr(settings, "local_mode", True)
    monkeypatch.setattr(settings, "api_host", "127.0.0.1")
    monkeypatch.setattr(settings, "port", 8110)
    monkeypatch.setattr(settings, "hosted_app_url", APP_ORIGIN)
    monkeypatch.setattr(settings, "hosted_api_url", API_ORIGIN)
    monkeypatch.setattr(settings, "byok_vault_key", SecretStr(Fernet.generate_key().decode()))
    monkeypatch.setattr(hosted_link, "_TRANSPORT", fake.transport())
    hosted_link.clear_used_states()
    return fake


@pytest.fixture
def engine() -> Engine:
    """Return an in-memory database with the connector table.

    Returns:
        The engine.
    """
    eng = create_engine("sqlite://", connect_args={"check_same_thread": False}, poolclass=StaticPool)
    UserConnectorModel.__table__.create(eng)
    return eng


@pytest.fixture
def client(engine: Engine) -> TestClient:
    """Return a client for an app with only the hosted-link router.

    Args:
        engine: Local database.

    Returns:
        The test client, signed in as the local user.
    """
    app = FastAPI()

    @app.exception_handler(DomainError)
    async def _domain_error_handler(_request: object, exc: DomainError) -> JSONResponse:
        """Mirror the app-level envelope so tests can assert on ``code``.

        Args:
            _request: The request (unused).
            exc: The raised error.

        Returns:
            The JSON error response.
        """
        return JSONResponse(status_code=exc.status_code, content={"code": exc.code, "params": exc.params})

    app.include_router(create_local_hosted_link_router(job_store=SimpleNamespace(engine=engine)))
    app.dependency_overrides[get_authenticated_user] = lambda: AuthenticatedUser(_USER, "user", ())
    return TestClient(app, raise_server_exceptions=False)


def _link(client: TestClient, hosted: FakeHosted) -> None:
    """Run start, consent and callback through the routes.

    Args:
        client: The test client.
        hosted: The fake hosted server.
    """
    url = client.post("/local/hosted-link/start", json={"device_name": "Test Mac"}).json()["authorize_url"]
    _redirect, code, state = hosted.authorize(url)
    response = client.get("/local/hosted-link/callback", params={"code": code, "state": state})
    assert response.status_code == 200
    assert html.escape(t_en("hosted_link.page_linked_title")) in response.text


def test_routes_are_hidden_outside_local_mode(client: TestClient, monkeypatch: pytest.MonkeyPatch) -> None:
    """Hosted deployments answer 404 on every hosted-link route.

    Args:
        client: The test client.
        monkeypatch: Pytest fixture used to turn local mode off.
    """
    monkeypatch.setattr(settings, "local_mode", False)

    assert client.post("/local/hosted-link/start", json={}).status_code == 404
    assert client.get("/local/hosted-link/callback", params={"code": "c", "state": "s"}).status_code == 404
    assert client.post("/local/hosted-link/finish", json={"code": "c", "state": "s"}).status_code == 404
    assert client.get("/local/hosted-link/status").status_code == 404
    assert client.post("/local/hosted-link/unlink").status_code == 404


def test_link_status_whoami_balance_unlink(client: TestClient, hosted: FakeHosted) -> None:
    """The full flow through the routes, with no token in any response.

    Args:
        client: The test client.
        hosted: The fake hosted server.
    """
    assert client.get("/local/hosted-link/status").json() == {"linked": False}

    _link(client, hosted)

    status = client.get("/local/hosted-link/status")
    assert status.json()["linked"] is True
    assert status.json()["account_email"] == "dana@example.test"
    who = client.get("/local/hosted-link/whoami")
    assert who.json()["email"] == "dana@example.test"
    assert who.json()["image"] == "https://lh3.googleusercontent.com/a/dana"
    balance = client.get("/local/hosted-link/balance")
    assert balance.json() == {"available_cents": 1234, "currency": "usd", "low_balance": False}
    for body in (status.text, who.text, balance.text):
        assert "skyl_" not in body
    unlink = client.post("/local/hosted-link/unlink")
    assert unlink.json() == {"hosted_revoked": True}
    assert client.get("/local/hosted-link/status").json() == {"linked": False}


def test_callback_errors_render_a_page(client: TestClient, hosted: FakeHosted) -> None:
    """A bad state or a declined consent shows a page with the error, not JSON.

    Args:
        client: The test client.
        hosted: The fake hosted server.
    """
    bad = client.get("/local/hosted-link/callback", params={"code": "c", "state": "forged"})
    declined = client.get("/local/hosted-link/callback", params={"error": "access_denied"})

    assert bad.status_code == 400
    assert "Start the link again" in bad.text
    assert html.escape(t_en("hosted_link.page_failed_title")) in bad.text
    assert "<script>" not in bad.text
    assert declined.status_code == 400
    assert hosted.calls == []


def test_revoked_link_answers_401(client: TestClient, hosted: FakeHosted) -> None:
    """After a website Remove, the Mac gets 401 ``hosted_link.revoked`` and is unlinked.

    Args:
        client: The test client.
        hosted: The fake hosted server.
    """
    _link(client, hosted)
    hosted.revoked = True

    response = client.get("/local/hosted-link/balance")

    assert response.status_code == 401
    assert response.json()["code"] == "hosted_link.revoked"
    assert client.get("/local/hosted-link/status").json() == {"linked": False}


def test_success_page_uses_the_catalog(client: TestClient, hosted: FakeHosted) -> None:
    """The success page shows the Mac, the account and a way back to the app.

    Args:
        client: The test client.
        hosted: The fake hosted server.
    """
    url = client.post("/local/hosted-link/start", json={"device_name": "Test Mac"}).json()["authorize_url"]
    _redirect, code, state = hosted.authorize(url)

    page = client.get("/local/hosted-link/callback", params={"code": code, "state": state})

    assert page.status_code == 200
    assert f"<title>{html.escape(APP_NAME)}</title>" in page.text
    for key in ("page_linked_title", "page_linked_lead", "page_device_label", "page_account_label"):
        assert html.escape(t_en(f"hosted_link.{key}")) in page.text
    assert "<dd>dana@example.test</dd>" in page.text
    assert f'href="skynet://open">{html.escape(t_en("hosted_link.page_open_app"))}</a>' in page.text
    assert "location.href='skynet://open'" in page.text


def test_connector_state_gives_the_state_invalid_page(client: TestClient, hosted: FakeHosted) -> None:
    """A connector OAuth state sent to the link callback gives a 400 page, not a 500.

    Args:
        client: The test client.
        hosted: The fake hosted server.
    """
    app = oauth.OAuthApp(
        provider="google_drive",
        authorize_url="https://accounts.example.test/authorize",
        token_url="https://accounts.example.test/token",
        scopes="drive.readonly",
        client_id="client-id",
        client_secret=None,
        extra_authorize_params={},
    )
    url = oauth.build_authorize_url(app, _USER, "http://127.0.0.1:8110/connectors/callback")
    state = parse_qs(urlsplit(url).query)["state"][0]

    page = client.get("/local/hosted-link/callback", params={"code": "c", "state": state})

    assert page.status_code == 400
    assert html.escape(t_en("hosted_link.state_invalid")) in page.text
    assert hosted.calls == []


def test_app_callback_flow_finishes_through_the_finish_route(client: TestClient, hosted: FakeHosted) -> None:
    """The Mac app asks for the skynet:// callback and posts the code and state it receives.

    Args:
        client: The test client.
        hosted: The fake hosted server.
    """
    url = client.post("/local/hosted-link/start", json={"device_name": "Test Mac", "app_callback": True}).json()[
        "authorize_url"
    ]
    redirect, code, state = hosted.authorize(url)
    assert redirect == hosted_link.APP_CALLBACK_URI == "skynet://link/callback"

    response = client.post("/local/hosted-link/finish", json={"code": code, "state": state})

    assert response.status_code == 200
    assert response.json() == {"linked": True, "account_email": "dana@example.test"}
    assert "skyl_" not in response.text
    assert client.get("/local/hosted-link/status").json()["linked"] is True
    replay = client.post("/local/hosted-link/finish", json={"code": code, "state": state})
    assert replay.status_code == 400
    assert replay.json()["code"] == "hosted_link.state_invalid"


def test_finish_refuses_a_forged_state(client: TestClient, hosted: FakeHosted) -> None:
    """A skynet:// URL with a state this Mac did not make never reaches hosted.

    Args:
        client: The test client.
        hosted: The fake hosted server.
    """
    response = client.post("/local/hosted-link/finish", json={"code": "c", "state": "forged"})

    assert response.status_code == 400
    assert response.json()["code"] == "hosted_link.state_invalid"
    assert hosted.calls == []
    assert client.get("/local/hosted-link/status").json() == {"linked": False}


def _consent_device_name(client: TestClient) -> str:
    """Start a link without a Mac name and return the name the consent URL carries.

    Args:
        client: The test client.

    Returns:
        The ``device_name`` query value.
    """
    url = client.post("/local/hosted-link/start", json={"app_callback": True}).json()["authorize_url"]
    return parse_qs(urlsplit(url).query)["device_name"][0]


def test_start_names_the_mac_by_its_computer_name(
    client: TestClient, hosted: FakeHosted, monkeypatch: pytest.MonkeyPatch
) -> None:
    """The consent screen shows the Computer Name, not the network host name.

    Args:
        client: The test client.
        hosted: The fake hosted server.
        monkeypatch: Pytest fixture used to fake ``scutil`` and the host name.
    """
    monkeypatch.setattr(local_hosted_link.platform, "node", lambda: "Gilads-MacBook-Pro.local")
    monkeypatch.setattr(
        local_hosted_link.subprocess,
        "run",
        lambda args, **_kwargs: subprocess.CompletedProcess(args, 0, stdout="Gilad’s MacBook Pro\n"),
    )

    assert _consent_device_name(client) == "Gilad’s MacBook Pro"


def test_start_reads_a_non_ascii_computer_name_under_an_ascii_locale(
    client: TestClient, hosted: FakeHosted, monkeypatch: pytest.MonkeyPatch
) -> None:
    """A curly apostrophe in the name still decodes when the login shell sets an ASCII locale.

    Args:
        client: The test client.
        hosted: The fake hosted server.
        monkeypatch: Pytest fixture used to fake ``scutil``.
    """

    def scutil(args: list[str], **kwargs: object) -> subprocess.CompletedProcess[str]:
        """Decode ``scutil``'s UTF-8 bytes the way ``subprocess.run`` does under ``LANG=en_US.US-ASCII``.

        Args:
            args: The command.
            **kwargs: ``subprocess.run`` options; ``encoding`` and ``errors`` decide the decoding.

        Returns:
            The finished process with the decoded output.
        """
        raw = "Gilad’s MacBook Pro\n".encode()
        stdout = raw.decode(str(kwargs.get("encoding") or "ascii"), str(kwargs.get("errors") or "strict"))
        return subprocess.CompletedProcess(args, 0, stdout=stdout)

    monkeypatch.setattr(local_hosted_link.subprocess, "run", scutil)
    assert _consent_device_name(client) == "Gilad’s MacBook Pro"


def test_start_falls_back_to_the_host_name_then_mac(
    client: TestClient, hosted: FakeHosted, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Without ``scutil``, the host name without ``.local`` is used, then ``Mac``.

    Args:
        client: The test client.
        hosted: The fake hosted server.
        monkeypatch: Pytest fixture used to fake ``scutil`` and the host name.
    """

    def missing_scutil(args: list[str], **_kwargs: object) -> subprocess.CompletedProcess[str]:
        """Fail like a system without ``scutil``.

        Args:
            args: The command.
            **_kwargs: Ignored ``subprocess.run`` options.

        Raises:
            FileNotFoundError: Always.
        """
        raise FileNotFoundError(args[0])

    monkeypatch.setattr(local_hosted_link.subprocess, "run", missing_scutil)
    monkeypatch.setattr(local_hosted_link.platform, "node", lambda: "build-host.local")
    assert _consent_device_name(client) == "build-host"

    monkeypatch.setattr(local_hosted_link.platform, "node", lambda: "")
    assert _consent_device_name(client) == "Mac"
