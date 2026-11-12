"""Tests for the local ``/local/hosted/*`` proxy against a fake hosted API."""

from __future__ import annotations

import html
import json
from types import SimpleNamespace

import httpx
import pytest
from cryptography.fernet import Fernet
from fastapi import FastAPI
from fastapi.responses import JSONResponse
from fastapi.testclient import TestClient
from pydantic import SecretStr
from sqlalchemy import create_engine
from sqlalchemy.engine import Engine
from sqlalchemy.pool import StaticPool

from ...config import settings
from ...i18n_en import t_en
from ...local import hosted_link, hosted_proxy
from ...local.tests.fake_hosted import API_ORIGIN, APP_ORIGIN, FakeHosted
from ...storage.models import UserConnectorModel
from ..auth import AuthenticatedUser, get_authenticated_user
from ..errors import DomainError
from ..routers.local_hosted_link import create_local_hosted_link_router
from ..routers.local_hosted_proxy import create_local_hosted_proxy_router

_USER = "local@skynet.local"


class ProxyHosted(FakeHosted):
    """The fake hosted link server plus a few web routes behind the desktop token."""

    def __init__(self) -> None:
        """Start with no seen proxied requests."""
        super().__init__()
        self.seen: list[httpx.Request] = []

    def _route(self, request: httpx.Request, path: str) -> httpx.Response:
        """Answer the web routes, and pass the link routes to the fake.

        Args:
            request: The incoming request.
            path: The URL path.

        Returns:
            The response.
        """
        if path.startswith("/desktop/"):
            return super()._route(request, path)
        self.calls.append((request.method, path))
        self.seen.append(request)
        denied = self._bearer(request)
        if denied is not None:
            return denied
        if path == "/optimizations":
            return httpx.Response(
                200,
                json={"items": [{"id": "run-1"}], "query": request.url.query.decode()},
                headers={"set-cookie": "hosted=1", "etag": '"v1"', "x-internal": "no"},
            )
        if path == "/optimizations/run-1/stream":
            body = b"id: 1\nevent: progress\ndata: {}\n\nid: 2\nevent: done\ndata: {}\n\n"
            return httpx.Response(200, content=body, headers={"content-type": "text/event-stream"})
        if path == "/agent/chats" and request.method == "POST":
            return httpx.Response(201, json={"echo": json.loads(request.content)})
        if path == "/billing/checkout":
            return self._problem(401, "auth.desktop_token_not_allowed")
        if path == "/optimizations/missing":
            return self._problem(404, "optimization.not_found")
        if path == "/projects/":
            return httpx.Response(307, headers={"location": f"{API_ORIGIN}/projects", "retry-after": "1"})
        if path == "/elsewhere":
            return httpx.Response(302, headers={"location": "https://other.test/x"})
        if path == "/moved/old":
            return httpx.Response(302, headers={"location": "new"})
        if path == "/odd-401":
            return self._problem(401, "auth.something_else")
        return httpx.Response(404, json={"code": "not_found"})


@pytest.fixture
def hosted(monkeypatch: pytest.MonkeyPatch) -> ProxyHosted:
    """Turn on local mode and point the link client at a fake hosted API.

    Args:
        monkeypatch: Pytest fixture used to change settings and the transport.

    Returns:
        The fake server.
    """
    fake = ProxyHosted()
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
    """Return a client for an app with the hosted-link and proxy routers.

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

    store = SimpleNamespace(engine=engine)
    app.include_router(create_local_hosted_link_router(job_store=store))
    app.include_router(create_local_hosted_proxy_router(job_store=store))
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


def test_proxy_is_hidden_outside_local_mode(client: TestClient, monkeypatch: pytest.MonkeyPatch) -> None:
    """Hosted deployments answer 404 on the proxy.

    Args:
        client: The test client.
        monkeypatch: Pytest fixture used to turn local mode off.
    """
    monkeypatch.setattr(settings, "local_mode", False)

    assert client.get("/local/hosted/optimizations").status_code == 404


def test_not_linked_answers_409(client: TestClient, hosted: ProxyHosted) -> None:
    """A Mac with no link gets 409 and hosted gets no call.

    Args:
        client: The test client.
        hosted: The fake hosted server.
    """
    response = client.get("/local/hosted/optimizations")

    assert response.status_code == 409
    assert response.json()["code"] == "hosted_link.not_linked"
    assert hosted.seen == []


def test_get_adds_token_keeps_query_and_filters_headers(client: TestClient, hosted: ProxyHosted) -> None:
    """The proxy adds the desktop token, drops cookies and passes only safe headers.

    Args:
        client: The test client.
        hosted: The fake hosted server.
    """
    _link(client, hosted)

    response = client.get(
        "/local/hosted/optimizations?limit=5&status=running",
        headers={"cookie": "local=1", "authorization": "Bearer local-jwt", "accept-language": "he"},
    )

    assert response.status_code == 200
    assert response.json()["items"] == [{"id": "run-1"}]
    assert response.json()["query"] == "limit=5&status=running"
    assert response.headers["etag"] == '"v1"'
    assert "set-cookie" not in response.headers
    assert "x-internal" not in response.headers
    sent = hosted.seen[-1]
    assert sent.url.host == "api.hosted.test"
    assert sent.headers["authorization"].startswith("Bearer skyl_at_")
    assert "cookie" not in sent.headers
    assert sent.headers["accept-language"] == "he"
    assert "skyl_" not in response.text


def test_post_body_and_status_pass_through(client: TestClient, hosted: ProxyHosted) -> None:
    """A JSON body reaches hosted, and the hosted status comes back.

    Args:
        client: The test client.
        hosted: The fake hosted server.
    """
    _link(client, hosted)

    response = client.post("/local/hosted/agent/chats", json={"title": "Hi"}, headers={"idempotency-key": "k-1"})

    assert response.status_code == 201
    assert response.json() == {"echo": {"title": "Hi"}}
    assert hosted.seen[-1].headers["idempotency-key"] == "k-1"


def test_hosted_errors_pass_through(client: TestClient, hosted: ProxyHosted) -> None:
    """A hosted 404 with its code reaches the renderer unchanged.

    Args:
        client: The test client.
        hosted: The fake hosted server.
    """
    _link(client, hosted)

    response = client.get("/local/hosted/optimizations/missing")

    assert response.status_code == 404
    assert response.json()["code"] == "optimization.not_found"


def test_event_stream_passes_through(client: TestClient, hosted: ProxyHosted) -> None:
    """Server-sent events stream back with their type and Last-Event-ID goes out.

    Args:
        client: The test client.
        hosted: The fake hosted server.
    """
    _link(client, hosted)

    with client.stream("GET", "/local/hosted/optimizations/run-1/stream", headers={"last-event-id": "1"}) as response:
        body = b"".join(response.iter_bytes())

    assert response.status_code == 200
    assert response.headers["content-type"].startswith("text/event-stream")
    assert b"event: done" in body
    assert hosted.seen[-1].headers["last-event-id"] == "1"


def test_expired_token_refreshes_and_resends_body(client: TestClient, hosted: ProxyHosted) -> None:
    """An expired access token refreshes once and the same body goes out again.

    Args:
        client: The test client.
        hosted: The fake hosted server.
    """
    _link(client, hosted)
    hosted.expired_access.update(hosted.access)

    response = client.post("/local/hosted/agent/chats", json={"title": "Again"})

    assert response.status_code == 201
    assert response.json() == {"echo": {"title": "Again"}}
    assert hosted.refresh_count == 1
    assert [json.loads(r.content) for r in hosted.seen] == [{"title": "Again"}, {"title": "Again"}]


def test_revoked_link_is_cleared(client: TestClient, hosted: ProxyHosted) -> None:
    """A link that hosted revoked answers 401 and the local link is gone.

    Args:
        client: The test client.
        hosted: The fake hosted server.
    """
    _link(client, hosted)
    hosted.revoked = True

    response = client.get("/local/hosted/optimizations")

    assert response.status_code == 401
    assert response.json()["code"] == "hosted_link.revoked"
    assert client.get("/local/hosted-link/status").json() == {"linked": False}


def test_unknown_token_after_refresh_clears_link(client: TestClient, hosted: ProxyHosted) -> None:
    """A fresh token that hosted does not know means a dead link.

    Args:
        client: The test client.
        hosted: The fake hosted server.
    """
    _link(client, hosted)
    hosted.reject_all_access = True

    response = client.get("/local/hosted/optimizations")

    assert response.status_code == 401
    assert response.json()["code"] == "hosted_link.revoked"
    assert hosted.refresh_count == 1


def test_closed_route_answers_403_and_keeps_link(client: TestClient, hosted: ProxyHosted) -> None:
    """A route hosted closes to the Mac token is a 403, not a sign-out.

    Args:
        client: The test client.
        hosted: The fake hosted server.
    """
    _link(client, hosted)

    response = client.post("/local/hosted/billing/checkout", json={})

    assert response.status_code == 403
    assert response.json()["code"] == "auth.desktop_token_not_allowed"
    assert client.get("/local/hosted-link/status").json()["linked"] is True
    assert hosted.refresh_count == 0


def test_unreachable_hosted_answers_503(client: TestClient, hosted: ProxyHosted) -> None:
    """A network error answers 503 and keeps the link.

    Args:
        client: The test client.
        hosted: The fake hosted server.
    """
    _link(client, hosted)
    hosted.reachable = False

    response = client.get("/local/hosted/optimizations")

    assert response.status_code == 503
    assert response.json()["code"] == "hosted_link.unreachable"
    assert client.get("/local/hosted-link/status").json()["linked"] is True


@pytest.mark.parametrize(
    "path",
    [
        "",
        "/evil.test/x",
        "a/../desktop/link/revoke",
        "a//b",
        "a\\b",
        "https:/evil.test",
        "./x",
        "a\nb",
        "runs/a?b",
        "runs/a#b",
        "desktop/link/revoke",
        "desktop",
        "%64esktop/v1/whoami",
        "runs/a%2Fb",
    ],
)
def test_unsafe_paths_are_refused(path: str) -> None:
    """Paths that could leave the hosted host or climb up are refused.

    Args:
        path: The candidate path.
    """
    assert hosted_proxy.safe_path(path) is False


def test_safe_paths_are_accepted() -> None:
    """Normal API paths, with colons after the first segment, pass."""
    assert hosted_proxy.safe_path("optimizations/run-1/stream")
    assert hosted_proxy.safe_path("agent/chats/c1:fork")


def test_encoded_climb_does_not_reach_link_routes(client: TestClient, hosted: ProxyHosted) -> None:
    """A percent-encoded ``..`` is decoded before the check and refused.

    Args:
        client: The test client.
        hosted: The fake hosted server.
    """
    _link(client, hosted)
    before = list(hosted.calls)

    response = client.post("/local/hosted/x/%2e%2e/desktop/link/revoke", json={})

    assert response.status_code == 404
    assert hosted.calls == before
    assert hosted.revoked is False


def test_redirect_points_at_the_proxy(client: TestClient, hosted: ProxyHosted) -> None:
    """A hosted redirect on the hosted host points at the proxy; one that leaves it loses its Location.

    Args:
        client: The test client.
        hosted: The fake hosted server.
    """
    _link(client, hosted)

    same_host = client.get("/local/hosted/projects/", follow_redirects=False)
    other_host = client.get("/local/hosted/elsewhere", follow_redirects=False)

    assert same_host.status_code == 307
    assert same_host.headers["location"] == "/local/hosted/projects"
    assert same_host.headers["retry-after"] == "1"
    assert other_host.status_code == 302
    assert "location" not in other_host.headers


def test_raw_query_passes_unchanged(client: TestClient, hosted: ProxyHosted) -> None:
    """The query reaches hosted as the renderer sent it.

    Args:
        client: The test client.
        hosted: The fake hosted server.
    """
    _link(client, hosted)

    response = client.get("/local/hosted/optimizations?q=a%20b&flag&tag=x&tag=y")

    assert response.json()["query"] == "q=a%20b&flag&tag=x&tag=y"


def test_other_hosted_401_answers_502_and_keeps_link(client: TestClient, hosted: ProxyHosted) -> None:
    """A hosted 401 with an unknown code is not a local sign-out.

    Args:
        client: The test client.
        hosted: The fake hosted server.
    """
    _link(client, hosted)

    response = client.get("/local/hosted/odd-401")

    assert response.status_code == 502
    assert response.json()["code"] == "hosted_link.failed"
    assert client.get("/local/hosted-link/status").json()["linked"] is True


def test_double_encoded_desktop_path_is_refused(engine: Engine, hosted: ProxyHosted, client: TestClient) -> None:
    """A path that still holds ``%`` after the route decoded it never reaches hosted.

    ``%2564esktop`` arrives at the route as ``%64esktop``; hosted would decode
    it again to ``desktop``. The TestClient decodes twice, so this test calls
    the check the route uses on the once-decoded path.

    Args:
        engine: Local database.
        hosted: The fake hosted server.
        client: The test client.
    """
    _link(client, hosted)

    assert hosted_proxy.safe_path("%64esktop/v1/whoami") is False


def test_non_ascii_header_is_dropped(engine: Engine, hosted: ProxyHosted, client: TestClient) -> None:
    """A non-ASCII forwarded header value is dropped, not a 500.

    Args:
        engine: Local database.
        hosted: The fake hosted server.
        client: The test client.
    """
    _link(client, hosted)

    answer = hosted_proxy.forward(
        engine, _USER, "GET", "optimizations", "", {"accept": "application/json", "accept-language": "h\u00e9"}, b""
    )
    answer.close()

    assert answer.status == 200
    assert hosted.seen[-1].headers["accept"] == "application/json"
    assert "accept-language" not in hosted.seen[-1].headers


def test_relative_redirect_resolves_against_the_request(client: TestClient, hosted: ProxyHosted) -> None:
    """A relative Location resolves against the hosted path that answered.

    Args:
        client: The test client.
        hosted: The fake hosted server.
    """
    _link(client, hosted)

    response = client.get("/local/hosted/moved/old", follow_redirects=False)

    assert response.headers["location"] == "/local/hosted/moved/new"
