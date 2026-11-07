"""Hosted remote MCP server: agents run optimizations on a Skynet account (SKY-229).

The server speaks stateless Streamable HTTP at the configured resource URL
(:attr:`core.config.Settings.mcp_resource_url`), for example
``https://mcp.skynetml.com/mcp``. A PR preview serves it under its API host at
``/remote-mcp``, so it never collides with the internal ``/mcp`` mount.

Every request needs an MCP OAuth access token (:mod:`core.api.mcp_oauth`). A
missing or refused token gets a 401 with an RFC 9728 ``WWW-Authenticate``
challenge, so the client can discover the authorization server.

The tools call the same service layer as the optimization routes. Skynet
optimizes repositories only (SKY-316), so ``check_run`` and ``start_run``
refuse DSPy program runs with a 410. There are no tools that move money.
"""

from __future__ import annotations

import json
import logging
from collections.abc import Callable, Sequence
from functools import partial
from typing import Annotated, Any
from urllib.parse import urlsplit

import httpx
from fastapi import FastAPI
from fastapi.responses import JSONResponse
from fastmcp import FastMCP
from fastmcp.exceptions import ToolError
from fastmcp.server.dependencies import get_http_request
from mcp.types import ToolAnnotations
from pydantic import Field
from starlette.concurrency import run_in_threadpool
from starlette.types import ASGIApp, Receive, Scope, Send

from ..i18n import DEFAULT_LOCALE, request_locale, t_locale, term
from .auth import AuthenticatedUser
from .converters import job_owner
from .errors import DomainError
from .mcp_mount import _chained_lifespan
from .mcp_oauth import (
    SCOPE_READ,
    SCOPE_WRITE,
    McpPrincipal,
    McpTokenError,
    challenge_headers,
    resolve_principal,
    resource_url,
)
from .response_limits import AGENT_DEFAULT_LIST, AGENT_MAX_LIST, clamp_limit
from .routers._helpers import _grant_role, build_summary, load_job_for_user
from .routers.constants import VALID_STATUSES
from .routers.optimizations._local import execution_budget_view
from .routers.optimizations.lifecycle import cancel_optimization
from .sharing_access import ShareRole, role_rank

logger = logging.getLogger(__name__)

_PRINCIPAL_STATE_KEY = "mcp_principal"
_TERMINAL_STATUSES = frozenset({"success", "failed", "cancelled", "stopped"})
_STORED_REASON_TERMS = ("cancellationReason", "pauseReason")

_INSTRUCTIONS = (
    "Skynet optimizes repositories against a metric. "
    "To start a repository run, call repo_draft_start and follow each step's next_tool: "
    "every step saves part of the run and reports what to fix. "
    "repo_draft_review prices the run; repo_draft_submit starts it after the user agrees to the price. "
    "check_run and start_run refuse DSPy program runs. "
    "Use list_runs and get_run to follow the account's runs until the status is terminal. stop_run ends a run early. "
    "Tools named platform_* call the Skynet API directly."
)

# Adds more tools to the server. It gets the server and the model catalog
# client factory. Feature modules that import from this module register
# through it, so this module never imports them.
McpExtension = Callable[[FastMCP, Callable[[], httpx.Client]], None]


def _principal() -> McpPrincipal:
    """Return the principal the auth wrapper stored on this request."""
    principal = get_http_request().scope.get("state", {}).get(_PRINCIPAL_STATE_KEY)
    if not isinstance(principal, McpPrincipal):
        raise ToolError("Not authenticated.")
    return principal


def _user(scope: str) -> AuthenticatedUser:
    """Return the account behind this request, if its token has ``scope``.

    An MCP token never carries admin rights, whatever the account's role.

    Args:
        scope: The scope the tool needs.

    Returns:
        The account as an ordinary user.

    Raises:
        ToolError: When the token was not granted ``scope``.
    """
    principal = _principal()
    if scope not in principal.scopes:
        raise ToolError(f"insufficient_scope: this connection was not granted {scope}. Reconnect and approve it.")
    return AuthenticatedUser(username=principal.username, role="user", groups=())


def _request_locale() -> str:
    """Return the locale to answer this request in: its Accept-Language, else English."""
    return request_locale(None, get_http_request().headers.get("accept-language"))


def _run_summary(job_data: dict[str, Any], locale: str) -> dict[str, Any]:
    """Return a run summary whose stored stop reason reads in ``locale``.

    The service stores a cancel or pause reason as the Hebrew base term. The
    web UI maps it back the same way (``localizeStoredReason``). Other
    messages pass through unchanged.

    Args:
        job_data: The run row.
        locale: Locale tag of the caller.

    Returns:
        The summary as JSON data.
    """
    summary = build_summary(job_data).model_dump(mode="json")
    summary["stop_reason"] = job_data.get("stop_reason")
    for key in _STORED_REASON_TERMS:
        if summary.get("message") == term(key):
            summary["message"] = t_locale(locale, f"{{term.{key}}}")
    return summary


def _domain_tool_error(error: DomainError) -> ToolError:
    """Turn a service-layer error into a tool error the agent can read.

    Args:
        error: The service-layer error.

    Returns:
        A tool error with the code, status and rendered detail.
    """
    return ToolError(json.dumps({"code": error.code, "status": error.status_code, "detail": error.detail}))


def _dspy_run_retired() -> ToolError:
    """Return the refusal for a new DSPy program run: runs optimize repositories (SKY-316).

    Returns:
        A tool error with code ``optimization.dspy_run_retired`` and status 410.
    """
    return _domain_tool_error(DomainError("optimization.dspy_run_retired", status=410))


def _run_access(job_store: Any, optimization_id: str, user: AuthenticatedUser, minimum: ShareRole) -> dict[str, Any]:
    """Load a run the account owns or was granted, at ``minimum`` role or above.

    ``load_job_with_role`` gives every admin-allowlisted username owner access
    to all runs. An MCP token never carries admin rights, so this check asks
    for ownership or a share grant again, without the admin path.

    Args:
        job_store: Store that holds the run.
        optimization_id: The run.
        user: Account that asks.
        minimum: Lowest role that may use the tool.

    Returns:
        The run row.

    Raises:
        DomainError: 404 when the account does not own the run and has no
            grant on it; 403 when its grant is below ``minimum``.
    """
    job_data = load_job_for_user(job_store, optimization_id, user)
    username = user.username.strip().lower()
    role = ShareRole.owner if job_owner(job_data) == username else _grant_role(job_store, optimization_id, username)
    if role is None:
        raise DomainError("optimization.not_found", status=404, optimization_id=optimization_id)
    if role_rank(role) < role_rank(minimum):
        raise DomainError(
            "optimization.insufficient_role",
            status=403,
            optimization_id=optimization_id,
            required=str(minimum),
            role=str(role),
        )
    return job_data


def get_run(
    optimization_id: str, user: AuthenticatedUser, *, job_store: Any, locale: str = DEFAULT_LOCALE
) -> dict[str, Any]:
    """Return a run's status, budget, and its result when it is terminal.

    Args:
        optimization_id: The run.
        user: Account that asks; must have access to the run.
        job_store: Store that holds the run.
        locale: Locale tag for the run's stored stop reason.

    Returns:
        The run summary, its execution budget and its result.
    """
    job_data = _run_access(job_store, optimization_id, user, ShareRole.viewer)
    summary = _run_summary(job_data, locale)
    status = str(summary.get("status"))
    result = job_data.get("result") if status in _TERMINAL_STATUSES else None
    return {
        "run": summary,
        "execution_budget": execution_budget_view(job_store, job_data),
        "result": result if isinstance(result, dict) else None,
        "terminal": status in _TERMINAL_STATUSES,
    }


def list_runs(
    limit: int,
    offset: int,
    status: str | None,
    user: AuthenticatedUser,
    *,
    job_store: Any,
    locale: str = DEFAULT_LOCALE,
) -> dict[str, Any]:
    """Return a page of the account's own runs, newest first.

    Args:
        limit: Page size.
        offset: Runs to skip.
        status: Optional exact status filter.
        user: Account whose runs are listed.
        job_store: Store that holds the runs.
        locale: Locale tag for the runs' stored stop reasons.

    Returns:
        The total, the page and its runs.

    Raises:
        ToolError: For an unknown status.
    """
    if status is not None and status not in VALID_STATUSES:
        raise ToolError(f"Unknown status. Use one of: {', '.join(sorted(VALID_STATUSES))}.")
    resolved = clamp_limit(limit)
    total = job_store.count_jobs(status=status, username=user.username)
    rows = job_store.list_jobs(status=status, username=user.username, limit=resolved, offset=offset)
    return {
        "total": total,
        "limit": resolved,
        "offset": offset,
        "runs": [_run_summary(row, locale) for row in rows],
    }


def stop_run(optimization_id: str, user: AuthenticatedUser, *, job_store: Any, worker: Any) -> dict[str, Any]:
    """Stop a run, like the Stop button.

    Args:
        optimization_id: The run.
        user: Account that asks; must be able to edit the run.
        job_store: Store that holds the run.
        worker: Background worker that runs it.

    Returns:
        The cancellation response.
    """
    _run_access(job_store, optimization_id, user, ShareRole.editor)
    return cancel_optimization(job_store, optimization_id, user, worker).model_dump(mode="json")


def build_remote_mcp(
    *,
    service: Any,
    job_store: Any,
    get_worker: Callable[[], Any],
    http_client: Callable[[], httpx.Client] | None = None,
    extensions: Sequence[McpExtension] = (),
) -> FastMCP:
    """Build the remote MCP server and its tools.

    Args:
        service: Optimization service handed to the worker.
        job_store: Store that holds runs, budgets and grants.
        get_worker: Returns the background worker.
        http_client: Factory for the model catalog client.
        extensions: Registrars that add more tools to the server.

    Returns:
        The FastMCP server.
    """
    make_client = http_client or (lambda: httpx.Client(timeout=10, follow_redirects=False, trust_env=False))
    mcp = FastMCP(name="Skynet", instructions=_INSTRUCTIONS)
    read_only = ToolAnnotations(readOnlyHint=True, openWorldHint=False)

    def guarded(work: Callable[[], dict[str, Any]]) -> dict[str, Any]:
        """Run a tool body and turn service-layer errors into tool errors.

        Args:
            work: The tool body.

        Returns:
            The tool result.

        Raises:
            ToolError: For any service-layer error.
        """
        try:
            return work()
        except DomainError as error:
            raise _domain_tool_error(error) from error

    # check_run and start_run stay listed so an agent that knows them gets a
    # clear refusal, not an unknown-tool error. Their spec was a DSPy program
    # run, which Skynet no longer starts (SKY-316).
    @mcp.tool(name="check_run", annotations=read_only)
    def check_run_tool(spec: dict[str, Any] | None = None) -> dict[str, Any]:
        """Refuse every spec: Skynet now optimizes repositories only. Start a repository run in the Skynet app."""
        _user(SCOPE_READ)
        raise _dspy_run_retired()

    @mcp.tool(
        name="start_run",
        annotations=ToolAnnotations(readOnlyHint=False, destructiveHint=False, idempotentHint=True),
    )
    def start_run_tool(
        spec: dict[str, Any] | None = None,
        max_cost_cents: int | None = None,
        idempotency_key: str | None = None,
    ) -> dict[str, Any]:
        """Refuse every spec: Skynet now optimizes repositories only. Start a repository run in the Skynet app."""
        _user(SCOPE_WRITE)
        raise _dspy_run_retired()

    @mcp.tool(name="get_run", annotations=read_only)
    def get_run_tool(optimization_id: Annotated[str, Field(max_length=64)]) -> dict[str, Any]:
        """Get a run's status, spend and, when it is finished, its result."""
        user = _user(SCOPE_READ)
        locale = _request_locale()
        return guarded(lambda: get_run(optimization_id, user, job_store=job_store, locale=locale))

    @mcp.tool(name="list_runs", annotations=read_only)
    def list_runs_tool(
        limit: Annotated[int, Field(ge=1, le=AGENT_MAX_LIST)] = AGENT_DEFAULT_LIST,
        offset: Annotated[int, Field(ge=0)] = 0,
        status: Annotated[
            str | None, Field(description="pending, validating, running, success, failed, cancelled, paused or stopped")
        ] = None,
    ) -> dict[str, Any]:
        """List the account's runs, newest first."""
        user = _user(SCOPE_READ)
        locale = _request_locale()
        return guarded(lambda: list_runs(limit, offset, status, user, job_store=job_store, locale=locale))

    @mcp.tool(
        name="stop_run",
        annotations=ToolAnnotations(readOnlyHint=False, destructiveHint=True, idempotentHint=True),
    )
    def stop_run_tool(optimization_id: Annotated[str, Field(max_length=64)]) -> dict[str, Any]:
        """Stop a running optimization. Spend stops, and the unused budget is released."""
        user = _user(SCOPE_WRITE)
        return guarded(lambda: stop_run(optimization_id, user, job_store=job_store, worker=get_worker()))

    for extend in extensions:
        extend(mcp, make_client)
    return mcp


async def _send_json(send: Send, status: int, body: dict[str, Any], headers: dict[str, str]) -> None:
    """Send a small JSON response on a raw ASGI channel.

    Args:
        send: ASGI send callable.
        status: HTTP status.
        body: JSON body.
        headers: Extra response headers.
    """
    response = JSONResponse(status_code=status, content=body, headers=headers)
    await response({"type": "http"}, _no_receive, send)


async def _no_receive() -> dict[str, Any]:
    """Stand in for ``receive`` on a response that reads no body."""
    return {"type": "http.disconnect"}


class RemoteMcpDispatch:
    """Send requests for the MCP resource URL to the remote MCP app, after auth.

    Only a request whose host and path equal the configured resource URL goes
    to the MCP app. Every other request goes to the API unchanged.
    """

    def __init__(self, app: ASGIApp, *, mcp_app: ASGIApp, job_store: Any) -> None:
        """Hold the API app, the MCP app and the store for token checks.

        Args:
            app: The API application.
            mcp_app: The Streamable HTTP app of the remote MCP server.
            job_store: Store that holds the grants and tokens.
        """
        self.app = app
        self.mcp_app = mcp_app
        self.job_store = job_store

    def _targets_mcp(self, scope: Scope) -> bool:
        """Return whether ``scope`` is a request for the MCP resource URL."""
        url = resource_url()
        if scope["type"] != "http" or not url:
            return False
        parts = urlsplit(url)
        host = ""
        for name, value in scope.get("headers", []):
            if name == b"host":
                host = value.decode("latin-1")
                break
        hostname = urlsplit(f"//{host}").hostname or ""
        wanted = parts.path.rstrip("/") or "/"
        return hostname == (parts.hostname or "") and (scope.get("path", "").rstrip("/") or "/") == wanted

    async def __call__(self, scope: Scope, receive: Receive, send: Send) -> None:
        """Route one ASGI call.

        Args:
            scope: ASGI scope.
            receive: ASGI receive callable.
            send: ASGI send callable.
        """
        if not self._targets_mcp(scope):
            await self.app(scope, receive, send)
            return
        token = ""
        for name, value in scope.get("headers", []):
            if name == b"authorization":
                scheme, _, credentials = value.decode("latin-1").partition(" ")
                if scheme.lower() == "bearer":
                    token = credentials.strip()
                break
        if not token:
            await _send_json(send, 401, {"error": "invalid_request"}, challenge_headers())
            return
        try:
            principal = await run_in_threadpool(resolve_principal, self.job_store, token)
        except McpTokenError:
            headers = challenge_headers()
            headers["WWW-Authenticate"] += ', error="invalid_token"'
            await _send_json(send, 401, {"error": "invalid_token"}, headers)
            return
        except DomainError as error:
            await _send_json(send, error.status_code, {"error": error.code, "detail": error.detail}, {})
            return
        inner = dict(scope)
        inner["path"] = "/"
        inner["raw_path"] = b"/"
        inner["root_path"] = ""
        inner["state"] = {**scope.get("state", {}), _PRINCIPAL_STATE_KEY: principal}
        await self.mcp_app(inner, receive, send)


def mount_remote_mcp(
    app: FastAPI,
    *,
    service: Any,
    job_store: Any,
    get_worker: Callable[[], Any],
    extensions: Sequence[McpExtension] = (),
) -> None:
    """Serve the remote MCP server at the resource URL and chain its lifespan.

    The dispatch is the outermost middleware, so MCP responses skip the API's
    GZip and cache middleware, which would buffer the stream.

    Args:
        app: The API application.
        service: Optimization service handed to the worker.
        job_store: Store that holds runs, budgets and grants.
        get_worker: Returns the background worker.
        extensions: Registrars that add more tools to the server.
    """
    mcp = build_remote_mcp(service=service, job_store=job_store, get_worker=get_worker, extensions=extensions)
    mcp_app = mcp.http_app(path="/", stateless_http=True, json_response=True)
    app.add_middleware(RemoteMcpDispatch, mcp_app=mcp_app, job_store=job_store)
    parent_lifespan = app.router.lifespan_context
    app.router.lifespan_context = partial(_chained_lifespan, mcp_app=mcp_app, parent_lifespan=parent_lifespan)
