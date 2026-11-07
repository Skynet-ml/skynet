"""Drive the MCP server connection routes against the local OAuth MCP server."""

from __future__ import annotations

from typing import Any
from urllib.parse import parse_qs, urlsplit

import httpx
import pytest
from fastapi import FastAPI
from fastapi.responses import JSONResponse
from fastapi.testclient import TestClient

from core.billing.mcp_server_presets import PRESETS
from core.billing.tests.test_mcp_oauth_client import OAuthServerState, connections, oauth_server  # noqa: F401
from core.config import settings

from ..auth import AuthenticatedUser
from ..errors import DomainError
from ..routers.mcp_connections import create_mcp_connections_router
from .conftest import bypass_auth

APP_URL = "https://app.example"


def _app(user: AuthenticatedUser | None = None) -> FastAPI:
    """Mount the routes for one user with the app's coded error envelope.

    Args:
        user: The caller, or ``None`` for the default test user.

    Returns:
        The app.
    """
    app = FastAPI()
    app.include_router(create_mcp_connections_router())
    bypass_auth(app, user=user)

    @app.exception_handler(DomainError)
    async def _domain_error(_request: Any, exc: DomainError) -> JSONResponse:
        """Answer with ``code`` at the top level, as the app envelope does.

        Args:
            _request: The request.
            exc: The coded error.

        Returns:
            The error answer.
        """
        return JSONResponse(status_code=exc.status_code, content={"detail": exc.detail, "code": exc.code})

    return app


@pytest.fixture
def api(connections: Any, monkeypatch: pytest.MonkeyPatch) -> TestClient:  # noqa: F811
    """Mount the routes for the test user on the bound connection store.

    Returns:
        A client that does not follow redirects.
    """
    monkeypatch.setattr(settings, "app_public_url", APP_URL)
    return TestClient(_app(), follow_redirects=False)


@pytest.fixture
def bob(api: TestClient) -> TestClient:
    """Give a client that signs in as another user on the same routes.

    Returns:
        A client for bob, who started no sign-in.
    """
    return TestClient(_app(AuthenticatedUser(username="bob", role="user", groups=())), follow_redirects=False)


def _approve(authorize_url: str) -> dict[str, str]:
    """Approve the sign-in on the server and read the callback query.

    Args:
        authorize_url: The URL the start route returned.

    Returns:
        The query the server sends back to the callback.
    """
    location = httpx.get(authorize_url, trust_env=False).headers["location"]
    assert location.startswith("http://testserver/mcp/connections/callback?")
    return {key: values[0] for key, values in parse_qs(urlsplit(location).query).items()}


def _app_query(answer: Any) -> dict[str, str]:
    """Read the web app query from a callback redirect.

    Args:
        answer: The callback answer.

    Returns:
        The query values.
    """
    assert answer.status_code == 303
    assert answer.headers["location"].startswith(f"{APP_URL}/?")
    return {key: values[0] for key, values in parse_qs(urlsplit(answer.headers["location"]).query).items()}


def _app_fragment(answer: Any) -> dict[str, str]:
    """Read the state and code that a callback redirect puts in the fragment.

    Args:
        answer: The callback answer.

    Returns:
        The finish request body.
    """
    assert _app_query(answer) == {"settings": "connectors"}
    fragment = parse_qs(urlsplit(answer.headers["location"]).fragment)
    return {"state": fragment["mcp_state"][0], "code": fragment["mcp_code"][0]}


def test_presets_list_the_picker(api: TestClient) -> None:
    """List every preset with its name, title and URL."""
    answer = api.get("/mcp/connections/presets")

    assert answer.status_code == 200
    names = [preset["name"] for preset in answer.json()["presets"]]
    assert names == list(PRESETS)


def test_connect_list_and_disconnect(api: TestClient, oauth_server: OAuthServerState) -> None:  # noqa: F811
    """Connect through start, callback and finish, list without tokens, then disconnect."""
    start = api.post("/mcp/connections/start", json={"name": "tracker", "url": f"{oauth_server.base}/mcp"})
    assert start.status_code == 200

    callback = api.get("/mcp/connections/callback", params=_approve(start.json()["authorize_url"]))
    assert oauth_server.token_forms == []
    finish = api.post("/mcp/connections/finish", json=_app_fragment(callback))

    assert (finish.status_code, finish.json()) == (200, {"name": "tracker"})
    listing = api.get("/mcp/connections").json()["connections"]
    assert [(item["name"], item["status"]) for item in listing] == [("tracker", "connected")]
    assert set(listing[0]) == {"name", "server_url", "status", "connected_at"}
    for token in oauth_server.access | oauth_server.refresh:
        assert token not in finish.text
        assert token not in api.get("/mcp/connections").text
    assert api.delete("/mcp/connections/tracker").status_code == 204
    assert api.get("/mcp/connections").json() == {"connections": []}
    gone = api.delete("/mcp/connections/tracker")
    assert (gone.status_code, gone.json()["code"]) == (404, "mcp_connection.not_connected")


def test_callback_reports_a_decline_and_finish_refuses_a_reused_state(
    api: TestClient,
    oauth_server: OAuthServerState,  # noqa: F811
) -> None:
    """Send the browser back with an error code, and refuse a second finish."""
    declined = api.get("/mcp/connections/callback", params={"error": "access_denied", "state": "x"})
    assert _app_query(declined)["mcp_error"] == "mcp_connection.declined"

    start = api.post("/mcp/connections/start", json={"name": "tracker", "url": f"{oauth_server.base}/mcp"})
    body = _app_fragment(api.get("/mcp/connections/callback", params=_approve(start.json()["authorize_url"])))
    assert api.post("/mcp/connections/finish", json=body).status_code == 200
    reused = api.post("/mcp/connections/finish", json=body)
    assert (reused.status_code, reused.json()["code"]) == (400, "mcp_connection.state_invalid")


def test_finish_refuses_a_sign_in_that_another_user_started(
    api: TestClient,
    bob: TestClient,
    oauth_server: OAuthServerState,  # noqa: F811
) -> None:
    """Answer 403 when bob finishes alice's sign-in, so his MCP account never joins hers."""
    start = api.post("/mcp/connections/start", json={"name": "tracker", "url": f"{oauth_server.base}/mcp"})
    body = _app_fragment(api.get("/mcp/connections/callback", params=_approve(start.json()["authorize_url"])))

    hijack = bob.post("/mcp/connections/finish", json=body)

    assert (hijack.status_code, hijack.json()["code"]) == (403, "mcp_connection.wrong_account")
    assert oauth_server.token_forms == []
    assert api.get("/mcp/connections").json() == {"connections": []}
    assert bob.get("/mcp/connections").json() == {"connections": []}


def test_callback_sends_only_codes_to_the_fixed_app_url(api: TestClient) -> None:
    """Drop the server's raw error text and any redirect target from the query."""
    raw = "access_denied"
    description = "<script>steal()</script> user said no"
    declined = api.get(
        "/mcp/connections/callback",
        params={"error": raw, "error_description": description, "state": "x", "redirect_uri": "https://evil.example"},
    )

    assert _app_query(declined) == {"settings": "connectors", "mcp_error": "mcp_connection.declined"}
    assert "evil" not in declined.headers["location"]
    assert "steal" not in declined.headers["location"]
    passed = api.get(
        "/mcp/connections/callback", params={"code": "c", "state": "s", "redirect_uri": "https://evil.example"}
    )
    assert _app_fragment(passed) == {"state": "s", "code": "c"}
    assert "evil" not in passed.headers["location"]
    unknown = api.post("/mcp/connections/finish", json={"state": "never-issued", "code": "c"})
    assert (unknown.status_code, unknown.json()["code"]) == (400, "mcp_connection.state_invalid")


def test_start_errors_carry_a_code(api: TestClient, oauth_server: OAuthServerState) -> None:  # noqa: F811
    """Answer 422 for a bad request and 409 for a server that needs pre-registration."""
    unknown = api.post("/mcp/connections/start", json={"preset": "nowhere"})
    assert (unknown.status_code, unknown.json()["code"]) == (422, "mcp_connection.invalid_preset")
    missing = api.post("/mcp/connections/start", json={"name": "tracker"})
    assert (missing.status_code, missing.json()["code"]) == (422, "mcp_connection.invalid_request")

    oauth_server.registration = False
    refused = api.post("/mcp/connections/start", json={"name": "tracker", "url": f"{oauth_server.base}/mcp"})

    assert refused.status_code == 409
    assert refused.json()["code"] == "mcp_connection.registration_required"
    assert "register first" in refused.json()["detail"]
