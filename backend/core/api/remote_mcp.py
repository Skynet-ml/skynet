"""Hosted remote MCP server: agents run optimizations on a Skynet account (SKY-229).

The server speaks stateless Streamable HTTP at the configured resource URL
(:attr:`core.config.Settings.mcp_resource_url`), for example
``https://mcp.skynetml.com/mcp``. A PR preview serves it under its API host at
``/remote-mcp``, so it never collides with the internal ``/mcp`` mount.

Every request needs an MCP OAuth access token (:mod:`core.api.mcp_oauth`). A
missing or refused token gets a 401 with an RFC 9728 ``WWW-Authenticate``
challenge, so the client can discover the authorization server.

The tools call the same service layer as ``POST /optimize`` and the
optimization routes. A run always gets a new execution budget with the
agent's ``max_cost_cents``, with the same reservation and billing as a run
from the UI. There are no tools that move money.
"""

from __future__ import annotations

import json
import logging
import math
from collections.abc import Callable, Sequence
from decimal import Decimal
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

from ..billing.budgets import BudgetConflictError, BudgetError, BudgetService
from ..billing.service import StripeBillingService
from ..config import settings
from ..i18n import DEFAULT_LOCALE, request_locale, t_locale, term
from ..models.unified import UnifiedRunRequest
from ..service_gateway.optimization.unified.contract import in_locale, static_contract_check
from ..service_gateway.optimization.unified.metric_model import resolve_metric_model
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
from .rate_limit import enforce_submission_rate
from .response_limits import AGENT_DEFAULT_LIST, AGENT_MAX_LIST, clamp_limit
from .routers._helpers import _grant_role, build_summary, load_job_for_user
from .routers.constants import VALID_STATUSES
from .routers.models import box_hold_cents, call_hold_cents
from .routers.optimizations._local import execution_budget_view
from .routers.optimizations.lifecycle import cancel_optimization
from .routers.submissions import (
    UNIFIED_WORKFLOW,
    _submit_within_proxy_timeout,
    resolve_submission_replay_keys,
    submit_unified_payload,
)
from .sharing_access import ShareRole, role_rank

logger = logging.getLogger(__name__)

_PRINCIPAL_STATE_KEY = "mcp_principal"
# Matches the wizard: a model call reserves at least one dollar, and the
# default limit is at least five dollars.
_MIN_CALL_HOLD_CENTS = 100
_MIN_SUGGESTED_LIMIT_CENTS = 500
_PENDING_RETRY_SECONDS = 5
_TERMINAL_STATUSES = frozenset({"success", "failed", "cancelled", "stopped"})
_STORED_REASON_TERMS = ("cancellationReason", "pauseReason")

_INSTRUCTIONS = (
    "Skynet optimizes prompts, programs and agents against a metric. "
    "Call check_run with a run spec first: it checks the spec without running it and suggests a spending limit. "
    "Then call start_run with the same spec and a max_cost_cents the user agreed to. "
    "To optimize a GitHub repository, call repo_draft_start and follow each step's next_tool: "
    "every step saves part of the run and reports what to fix. "
    "repo_draft_review prices the run; repo_draft_submit starts it after the user agrees to the price. "
    "Poll get_run until the status is terminal. stop_run ends a run early. "
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


def _effective_sources(spec: UnifiedRunRequest) -> list[tuple[str, str]]:
    """List the ``(model, token_source)`` of every model role of a run spec.

    Args:
        spec: The run spec.

    Returns:
        One pair for each model the run names.
    """
    models = spec.models
    if models.reflection_model.token_source is None:
        models.reflection_model.token_source = spec.token_source
    resolve_metric_model(models, spec.workspace.metric_code)
    roles = [models.task_model, models.reflection_model, *models.extra_reflection_models, models.metric_model]
    return [(model.name, model.token_source or spec.token_source) for model in roles if model is not None]


def _floor_cents(spec: UnifiedRunRequest, http_client: Callable[[], httpx.Client]) -> tuple[int, list[str]]:
    """Estimate the smallest spending limit a run spec can start with.

    The floor is what the wizard requires: the boxes' hold plus one call of
    the dearest model.

    Args:
        spec: The run spec.
        http_client: Factory for the model catalog client.

    Returns:
        The floor in cents, and the models the catalog has no price for.
    """
    sources = _effective_sources(spec)
    boxes = math.ceil(box_hold_cents())
    unpriced: list[str] = []
    if all(source == "subscription" for _, source in sources):
        return boxes, unpriced
    holds: list[Decimal] = []
    with http_client() as client:
        for name, source in sources:
            hold = call_hold_cents(name, source, client=client)
            if hold is None:
                unpriced.append(name)
            else:
                holds.append(hold)
    return boxes + max(_MIN_CALL_HOLD_CENTS, math.ceil(max(holds, default=Decimal(0)))), unpriced


def check_run(
    spec: UnifiedRunRequest,
    user: AuthenticatedUser,
    *,
    job_store: Any,
    http_client: Callable[[], httpx.Client],
    locale: str = DEFAULT_LOCALE,
) -> dict[str, Any]:
    """Check a run spec without running it, and estimate its spending floor.

    The floor is what the wizard requires: the boxes' hold plus one call of
    the dearest model. A run has no upfront total estimate.

    Args:
        spec: The run spec.
        user: Account that will run it.
        job_store: Store whose engine backs the balance read.
        http_client: Factory for the model catalog client.
        locale: Locale tag for the check messages.

    Returns:
        The static checks, the floor, a suggested limit and the balance.
    """
    enforce_submission_rate(user.username)
    checks = in_locale(static_contract_check(spec), locale)
    floor, unpriced = _floor_cents(spec, http_client)
    return {
        "checks": checks.model_dump(mode="json"),
        "min_max_cost_cents": floor,
        "suggested_max_cost_cents": max(_MIN_SUGGESTED_LIMIT_CENTS, floor),
        "unpriced_models": unpriced,
        "spendable_cents": StripeBillingService(engine=job_store.engine).spendable_cents(user.username),
        "note": "A run stops when it reaches max_cost_cents. Unused budget is released when the run ends.",
    }


def _refuse_lower_limit(job_store: Any, username: str, budget_key: str, max_cost_cents: int) -> None:
    """Refuse a retry that asks for less than the budget its key already made.

    The submission reuses the budget a known key made, so the run would keep
    the higher limit without a word. A retry that asks for more is allowed:
    the submission lowers a first limit to what the balance can back. A retry
    with the first limit is allowed too, also after the user raised the
    total in the app: ``BudgetService.create`` returns the existing budget
    unchanged when ``max_cost_cents`` equals its creation total. Known limit:
    the budget stores only a hash of the creation total. If the balance cap
    lowered the first limit and the user then raised the total, the original
    request gets this refusal, which names the run.

    Args:
        job_store: Store whose engine holds the budgets.
        username: Account that asks.
        budget_key: Budget creation key of the agent's idempotency key.
        max_cost_cents: Spending limit of this call.

    Raises:
        ToolError: When the key's budget total is above ``max_cost_cents``.
    """
    engine = getattr(job_store, "engine", None)
    if engine is None:
        return
    service = BudgetService(engine=engine)
    try:
        existing = service.find_by_creation_key(username, budget_key)
        if existing is None or max_cost_cents >= existing.total_cents:
            return
        service.create(username, max_cost_cents, idempotency_key=budget_key, uncapped=settings.local_mode)
    except BudgetConflictError as error:
        detail = f"This idempotency_key already has a limit of {existing.total_cents} cents. "
        payload: dict[str, Any] = {"code": "budget.conflict", "status": 409}
        if existing.job_id:
            payload["optimization_id"] = existing.job_id
            detail += "Use get_run or stop_run on optimization_id, or a new key for a run with a lower limit."
        else:
            detail += "Use a new key for a run with a lower limit."
        payload["detail"] = detail
        raise ToolError(json.dumps(payload)) from error
    except BudgetError:
        # The submission reads the same wallet next and reports this with its own code.
        return


def _below_minimum_error(
    error: DomainError,
    spec: UnifiedRunRequest,
    max_cost_cents: int,
    idempotency_key: str | None,
    http_client: Callable[[], httpx.Client],
) -> ToolError:
    """Name the smallest limit the run can start with in a too-low-limit refusal.

    Args:
        error: The submission's ``budget.below_minimum`` refusal.
        spec: The run spec.
        max_cost_cents: Spending limit the agent asked for.
        idempotency_key: Key the agent sent, if any.
        http_client: Factory for the model catalog client.

    Returns:
        A tool error with ``min_max_cost_cents``, the floor ``check_run`` reports.
    """
    floor, _unpriced = _floor_cents(spec, http_client)
    detail = error.detail
    # The floor is an estimate; when the setup check needed more, do not name
    # a minimum the agent already met.
    if floor > max_cost_cents:
        detail = f"max_cost_cents {max_cost_cents} is below this run's minimum of {floor} cents. Use at least {floor}."
    if idempotency_key:
        # A same-key retry reuses the budget this attempt created, with its
        # low limit, so it would fail the same way every time.
        detail += " Retry with a new idempotency_key; this key keeps its first limit."
    payload = {"code": error.code, "status": error.status_code, "detail": detail, "min_max_cost_cents": floor}
    return ToolError(json.dumps(payload))


def start_run(
    spec: UnifiedRunRequest,
    max_cost_cents: int,
    idempotency_key: str | None,
    user: AuthenticatedUser,
    *,
    service: Any,
    job_store: Any,
    http_client: Callable[[], httpx.Client],
) -> dict[str, Any]:
    """Start a run under a new execution budget of ``max_cost_cents``.

    Budget and setup-check fields of the spec are cleared, so the run always
    gets a fresh budget with the agent's limit, like an API submission.

    Args:
        spec: The run spec.
        max_cost_cents: Spending limit of the new budget.
        idempotency_key: Optional key; a repeat returns the original run.
        user: Account that runs it.
        service: Optimization service handed to the worker.
        job_store: Store that persists the run.
        http_client: Factory for the model catalog client that prices the run's floor.

    Returns:
        The submission, or ``status: in_progress`` while its setup check runs or
        other usage on the account settles.

    Raises:
        ToolError: When ``idempotency_key`` first ran with a higher limit, or
            when the limit is below the run's minimum.
    """
    spec.max_cost_cents = max_cost_cents
    spec.execution_budget_id = None
    spec.execution_budget_revision = None
    spec.execution_budget_generation = None
    spec.preflight_id = None
    spec.preflight_fingerprint = None
    replay_keys = resolve_submission_replay_keys(
        spec, username=user.username, workflow=UNIFIED_WORKFLOW, supplied_key=idempotency_key
    )
    if idempotency_key and replay_keys.budget is not None:
        _refuse_lower_limit(job_store, user.username, replay_keys.budget, max_cost_cents)
    replay_key = replay_keys.job
    try:
        result = _submit_within_proxy_timeout(
            user.username,
            replay_key,
            lambda: submit_unified_payload(
                spec, service=service, job_store=job_store, current_user=user, idempotency_key=idempotency_key
            ),
        )
    except DomainError as error:
        if error.code == "budget.below_minimum":
            raise _below_minimum_error(error, spec, max_cost_cents, idempotency_key, http_client) from error
        # budget.pending means earlier usage awaits confirmation or another run
        # holds the wallet funds. Either can clear later, and the same call
        # then goes through, so report it like a running setup check.
        if error.code != "budget.pending":
            raise
        return {
            "status": "in_progress",
            "retry_after_seconds": _PENDING_RETRY_SECONDS,
            "detail": "Other usage on this account must settle first. Call start_run again with the same arguments.",
        }
    if isinstance(result, JSONResponse):
        return {
            "status": "in_progress",
            "retry_after_seconds": int(result.headers.get("Retry-After", "5")),
            "detail": "The setup check is still running. Call start_run again with the same arguments.",
        }
    return result.model_dump(mode="json")


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

    @mcp.tool(name="check_run", annotations=read_only)
    def check_run_tool(spec: UnifiedRunRequest) -> dict[str, Any]:
        """Check a run spec without running or charging it, and suggest a spending limit.

        Returns the static checks of the workspace code, the smallest
        max_cost_cents the run can start with, a suggested limit, and the
        account's spendable balance in cents.
        """
        user = _user(SCOPE_READ)
        locale = _request_locale()
        return guarded(lambda: check_run(spec, user, job_store=job_store, http_client=make_client, locale=locale))

    @mcp.tool(
        name="start_run",
        annotations=ToolAnnotations(readOnlyHint=False, destructiveHint=False, idempotentHint=True),
    )
    def start_run_tool(
        spec: UnifiedRunRequest,
        max_cost_cents: Annotated[int, Field(ge=1, le=1_000_000, description="Spending limit of this run, in cents.")],
        idempotency_key: Annotated[
            str | None, Field(max_length=128, description="Send the same key to retry without a second run.")
        ] = None,
    ) -> dict[str, Any]:
        """Start an optimization run that spends at most max_cost_cents from the account balance.

        Call check_run first. Ask the user to agree to max_cost_cents. If the
        result says in_progress, call again with the same arguments.
        """
        user = _user(SCOPE_WRITE)
        return guarded(
            lambda: start_run(
                spec,
                max_cost_cents,
                idempotency_key,
                user,
                service=service,
                job_store=job_store,
                http_client=make_client,
            )
        )

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
