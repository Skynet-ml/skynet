"""Routes that connect a user's account to a remote MCP server with OAuth.

``POST /mcp/connections/start`` returns the server's sign-in URL. The browser
comes back to ``GET /mcp/connections/callback``, which passes the state and
code to the web app in the URL fragment. The signed-in web app then calls
``POST /mcp/connections/finish``, which checks that the caller started the
sign-in and stores the tokens encrypted. A run payload names the connection
in ``env_connectors``, and the broker on the parent adds the token to each
tool call, so the token never reaches the box. The ``[services.<name>]``
wiring in optimize.toml comes with the SKY-354 follow-up.
"""

from __future__ import annotations

from datetime import datetime
from typing import Annotated
from urllib.parse import urlencode

from fastapi import APIRouter, Depends, Request, Response
from fastapi.responses import RedirectResponse
from pydantic import BaseModel, Field

from ...billing.mcp_oauth_client import (
    McpOAuthError,
    disconnect,
    finish_connection,
    list_connections,
    start_connection,
)
from ...billing.mcp_server_presets import PRESETS
from ...config import settings
from ..auth import AuthenticatedUser, get_authenticated_user
from ..errors import DomainError


# One remote MCP server the picker offers.
class McpServerPresetResponse(BaseModel):
    name: str
    title: str
    url: str


# The picker's list of remote MCP servers.
class McpServerPresetListResponse(BaseModel):
    presets: list[McpServerPresetResponse]


# One finished connection, without tokens.
class McpConnectionResponse(BaseModel):
    name: str
    server_url: str
    status: str
    connected_at: datetime


# The caller's connections, by name.
class McpConnectionListResponse(BaseModel):
    connections: list[McpConnectionResponse]


# Start a sign-in with a preset, or with a name and any MCP server URL.
class McpConnectionStartRequest(BaseModel):
    preset: str | None = Field(default=None, max_length=40)
    name: str | None = Field(default=None, max_length=40)
    url: str | None = Field(default=None, max_length=2048)


# Where the browser goes to sign in.
class McpConnectionStartResponse(BaseModel):
    authorize_url: str


# The state and code that the callback passed to the web app.
class McpConnectionFinishRequest(BaseModel):
    state: str = Field(max_length=200)
    code: str = Field(max_length=2048)


# The connector name of the finished connection.
class McpConnectionFinishResponse(BaseModel):
    name: str


def _http_error(error: McpOAuthError) -> DomainError:
    """Turn a sign-in failure into its HTTP answer.

    A ``DomainError`` puts ``code`` and ``params`` at the top of the error
    envelope, where the web app reads them to show the localized message.

    Args:
        error: The failure with its i18n key and params.

    Returns:
        The exception to raise.
    """
    return DomainError(error.code, status=error.status, params=error.params)


def _app_redirect(fragment: dict[str, str] | None = None, **params: str) -> RedirectResponse:
    """Send the browser back to the connectors settings of the web app.

    Args:
        fragment: Values for the URL fragment, which the browser does not send
            to any server or put in a ``Referer`` header.
        **params: Result values for the query.

    Returns:
        A 303 redirect into the frontend.
    """
    query = urlencode({"settings": "connectors", **params})
    tail = f"#{urlencode(fragment)}" if fragment else ""
    return RedirectResponse(f"{settings.app_public_url.rstrip('/')}/?{query}{tail}", status_code=303)


def create_mcp_connections_router() -> APIRouter:
    """Build the MCP server connection routes.

    Returns:
        A router under ``/mcp/connections``.
    """
    router = APIRouter()

    @router.get(
        "/mcp/connections/presets",
        response_model=McpServerPresetListResponse,
        summary="List the remote MCP servers in the picker",
    )
    def list_mcp_presets(
        user: Annotated[AuthenticatedUser, Depends(get_authenticated_user)],
    ) -> McpServerPresetListResponse:
        """Return the remote MCP servers a user can connect with one click.

        Args:
            user: Authenticated caller.

        Returns:
            The presets.
        """
        return McpServerPresetListResponse(
            presets=[McpServerPresetResponse(name=p.name, title=p.title, url=p.url) for p in PRESETS.values()]
        )

    @router.get(
        "/mcp/connections",
        response_model=McpConnectionListResponse,
        summary="List the caller's MCP server connections",
    )
    def list_mcp_connections(
        user: Annotated[AuthenticatedUser, Depends(get_authenticated_user)],
    ) -> McpConnectionListResponse:
        """Return the caller's connections without tokens.

        Args:
            user: Authenticated caller.

        Returns:
            The connections.
        """
        return McpConnectionListResponse(
            connections=[
                McpConnectionResponse(
                    name=view.name, server_url=view.server_url, status=view.status, connected_at=view.connected_at
                )
                for view in list_connections(user.username)
            ]
        )

    @router.post(
        "/mcp/connections/start",
        response_model=McpConnectionStartResponse,
        summary="Start the OAuth sign-in to a remote MCP server",
    )
    def start_mcp_connection(
        body: McpConnectionStartRequest,
        request: Request,
        user: Annotated[AuthenticatedUser, Depends(get_authenticated_user)],
    ) -> McpConnectionStartResponse:
        """Discover the server, register Skynet with it and return its sign-in URL.

        Args:
            body: A preset, or a name and a server URL.
            request: Incoming request, for the callback URL.
            user: Authenticated caller.

        Returns:
            The authorization URL.

        Raises:
            DomainError: 422 for an unknown preset or a bad name or URL; 409
                when the server needs a pre-registered client; 502 when
                discovery fails.
        """
        if body.preset is not None:
            preset = PRESETS.get(body.preset)
            if preset is None:
                raise _http_error(McpOAuthError("mcp_connection.invalid_preset", 422))
            name, url = body.name or preset.name, preset.url
        elif body.name and body.url:
            name, url = body.name, body.url
        else:
            raise _http_error(McpOAuthError("mcp_connection.invalid_request", 422))
        redirect_uri = str(request.url_for("mcp_connection_callback"))
        try:
            authorize_url = start_connection(user.username, name, url, redirect_uri)
        except McpOAuthError as error:
            raise _http_error(error) from error
        return McpConnectionStartResponse(authorize_url=authorize_url)

    @router.get("/mcp/connections/callback", name="mcp_connection_callback", include_in_schema=False)
    def mcp_connection_callback(
        code: str | None = None,
        state: str | None = None,
        error: str | None = None,
    ) -> RedirectResponse:
        """Pass the state and code to the web app, which finishes the sign-in.

        This route has no session, because auth is a bearer header. So it
        trades no code itself: the signed-in finish route checks that the
        caller started the sign-in, which blocks an account-link hijack.

        Args:
            code: Authorization code.
            state: The state from the start step.
            error: The server's error code when the user declined.

        Returns:
            A redirect with an error code, or with the state and code in the fragment.
        """
        if error or not code or not state:
            return _app_redirect(mcp_error="mcp_connection.declined")
        return _app_redirect({"oauth_flow": "mcp", "oauth_state": state, "oauth_code": code})

    @router.post(
        "/mcp/connections/finish",
        response_model=McpConnectionFinishResponse,
        summary="Finish the OAuth sign-in to a remote MCP server",
    )
    def finish_mcp_connection(
        body: McpConnectionFinishRequest,
        user: Annotated[AuthenticatedUser, Depends(get_authenticated_user)],
    ) -> McpConnectionFinishResponse:
        """Trade the code for tokens when the caller started this sign-in.

        Args:
            body: The state and code from the callback.
            user: Authenticated caller.

        Returns:
            The connector name.

        Raises:
            DomainError: 400 for an unknown, used or old state; 403 when
                another user started the sign-in; 502 when the exchange fails;
                503 when the vault is not configured.
        """
        try:
            name = finish_connection(user.username, body.state, body.code)
        except McpOAuthError as error:
            raise _http_error(error) from error
        return McpConnectionFinishResponse(name=name)

    @router.delete(
        "/mcp/connections/{name}",
        status_code=204,
        summary="Disconnect a remote MCP server and delete its tokens",
    )
    def delete_mcp_connection(
        name: str,
        user: Annotated[AuthenticatedUser, Depends(get_authenticated_user)],
    ) -> Response:
        """Revoke the tokens when the server allows it, then delete them.

        Args:
            name: Connector name.
            user: Authenticated caller.

        Returns:
            An empty 204 answer.

        Raises:
            DomainError: 404 when the caller has no such connection.
        """
        if not disconnect(user.username, name):
            raise _http_error(McpOAuthError("mcp_connection.not_connected", 404, name=name))
        return Response(status_code=204)

    return router
