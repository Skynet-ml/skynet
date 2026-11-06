"""Platform tools for the remote MCP server (SKY-320).

The remote MCP server gets one ``platform_*`` tool for each REST route that
carries the ``"agent"`` OpenAPI tag, the same routes the in-app agent uses.
Each tool call goes in process to the REST route with a short-lived backend
session token for the MCP principal, so the route applies its usual account
checks. A read route needs the ``runs:read`` scope and any other method
needs ``runs:write``.

Some agent-tagged routes stay out: routes that start paid work without the
cost confirmation of the repo draft flow, and routes that only drive the
in-app UI.
"""

from __future__ import annotations

import base64
import hashlib
import hmac
import json
import time
from collections.abc import Generator
from typing import TYPE_CHECKING

import httpx
from fastmcp import FastMCP
from fastmcp.exceptions import ToolError
from fastmcp.server.providers.openapi.routing import MCPType, RouteMap

from ..config import settings
from .mcp_mount import AGENT_TAG, _trim_tool_spec
from .mcp_oauth import SCOPE_READ, SCOPE_WRITE
from .remote_mcp import McpExtension, _principal

if TYPE_CHECKING:
    from fastapi import FastAPI

NAMESPACE = "platform"

_READ_METHODS = frozenset({"GET", "HEAD", "OPTIONS"})
_SESSION_TOKEN_SECONDS = 60

# Each regex is matched against the whole route path. The paid run routes
# start work without the cost review and confirmation that repo_draft_submit
# enforces. The UI routes open cards, pages or settings dialogs in the web app
# and do nothing useful for an outside client.
_EXCLUDED_ROUTES: tuple[tuple[str, str], ...] = (
    ("POST", r"^/optimize$"),
    ("POST", r"^/optimizations/\{optimization_id\}/(clone|retry|resume|restart)$"),
    ("POST", r"^/serve/.*$"),
    ("POST", r"^/agent/(protected-settings|open-page)$"),
    ("POST", r"^/settings/user-preferences$"),
    ("POST", r"^/wizard/update$"),
    ("POST", r"^/datasets/(request-upload|request-library-pick)$"),
    ("POST", r"^/billing/.*$"),
    ("PUT", r"^/billing/.*$"),
    ("PATCH", r"^/billing/.*$"),
    ("DELETE", r"^/billing/.*$"),
)


def _b64url(data: bytes) -> str:
    """Encode ``data`` as unpadded base64url text.

    Args:
        data: Bytes to encode.

    Returns:
        The encoded text.
    """
    return base64.urlsafe_b64encode(data).rstrip(b"=").decode("ascii")


def session_token(username: str, secret: str, now: int | None = None) -> str:
    """Sign a short-lived backend session token for ``username``.

    The token has the shape that ``get_authenticated_user`` accepts from the
    web app. It never carries an admin role or groups.

    Args:
        username: The account the token acts for.
        secret: The backend auth secret.
        now: The issue time in Unix seconds; the current time when omitted.

    Returns:
        The signed HS256 token.
    """
    issued = int(time.time()) if now is None else now
    header = _b64url(json.dumps({"alg": "HS256", "typ": "JWT"}, separators=(",", ":")).encode())
    payload = _b64url(
        json.dumps(
            {
                "aud": "skynet-backend",
                "iss": "skynet-frontend",
                "name": username,
                "role": "user",
                "iat": issued,
                "exp": issued + _SESSION_TOKEN_SECONDS,
            },
            separators=(",", ":"),
        ).encode()
    )
    signature = hmac.new(secret.encode("utf-8"), f"{header}.{payload}".encode("ascii"), hashlib.sha256).digest()
    return f"{header}.{payload}.{_b64url(signature)}"


class _PrincipalSessionAuth(httpx.Auth):
    """Sign each projected route call as the MCP principal.

    The auth flow replaces the forwarded MCP bearer token, which the REST
    routes do not accept, with a backend session token for the same account.
    """

    def auth_flow(self, request: httpx.Request) -> Generator[httpx.Request, httpx.Response, None]:
        """Check the scope and set the session token on ``request``.

        Args:
            request: The in-process REST request.

        Yields:
            The signed request.

        Raises:
            ToolError: When the token lacks the scope, the account is an
                admin account, or the server has no auth secret.
        """
        principal = _principal()
        scope = SCOPE_READ if request.method in _READ_METHODS else SCOPE_WRITE
        if scope not in principal.scopes:
            raise ToolError(f"insufficient_scope: this connection was not granted {scope}. Reconnect and approve it.")
        # The allowlist makes any session of these accounts an admin session,
        # whatever role the token states, so an MCP token must not reach them.
        if principal.username in settings.admin_usernames_set:
            raise ToolError("Platform tools are not available for admin accounts.")
        if settings.backend_auth_secret is None:
            raise ToolError("Platform tools are not configured on this server.")
        token = session_token(principal.username, settings.backend_auth_secret.get_secret_value())
        request.headers["Authorization"] = f"Bearer {token}"
        yield request


def build_route_server(app: FastAPI) -> FastMCP:
    """Build the MCP server that projects the agent-tagged routes of ``app``.

    Args:
        app: The FastAPI app, with all routers included.

    Returns:
        The FastMCP server of the platform tools.
    """
    excluded = [
        RouteMap(methods=[method], pattern=pattern, mcp_type=MCPType.EXCLUDE) for method, pattern in _EXCLUDED_ROUTES
    ]
    return FastMCP.from_fastapi(
        app=app,
        name="Skynet platform",
        route_maps=[
            *excluded,
            RouteMap(tags={AGENT_TAG}, mcp_type=MCPType.TOOL),
            RouteMap(mcp_type=MCPType.EXCLUDE),
        ],
        mcp_component_fn=_trim_tool_spec,
        # The base URL host must pass the local-mode TrustedHostMiddleware.
        httpx_client_kwargs={"base_url": "http://127.0.0.1", "auth": _PrincipalSessionAuth()},
    )


def register_route_tools(app: FastAPI) -> McpExtension:
    """Return a registrar that mounts the platform tools of ``app``.

    Args:
        app: The FastAPI app, with all routers included.

    Returns:
        The registrar for ``build_remote_mcp``.
    """

    def register(mcp: FastMCP, _http_client: object) -> None:
        """Mount the platform tools on ``mcp``.

        Args:
            mcp: The remote MCP server.
            _http_client: The model catalog client factory; not used.
        """
        mcp.mount(build_route_server(app), namespace=NAMESPACE)

    return register
