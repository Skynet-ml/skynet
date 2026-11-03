"""Broker optimizer model calls through a trusted, account-scoped parent process."""

from __future__ import annotations

import copy
import json
import logging
import re
import secrets
import threading
import time
from collections.abc import Callable, Mapping
from dataclasses import asdict
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from typing import Any, Protocol
from urllib.parse import urlsplit

import httpx

from .. import config as app_config
from ..config import VERCEL_SANDBOX_LIFETIME_CEILING_SECONDS, app_env
from ..exceptions import DETERMINISTIC_FAILURE, INFRASTRUCTURE_INTERRUPTION, InfrastructureInterruptionError
from ..i18n_en import t_en
from ..local.cli_catalog import CliProvider, parse_subscription_model_id
from .budgets import BudgetError, BudgetInsufficientError
from .byok_upstreams import (
    OPENROUTER,
    ProviderUpstream,
    byok_model_prefix,
    resolve_upstream,
    validate_byok_novelty,
    validate_custom_base,
)
from .cli_dispatch import CliDispatcher
from .credential_safety import scrub_model_config
from .data_policy import data_policy_for
from .dependency_lock import verify_dependency_lock
from .mcp_broker import McpToolsBroker, _pinned_address, _PinnedTransport
from .model_batch import BatchCollector
from .model_dispatch import MODEL_ATTEMPT_HEADER, ModelHTTPResult, OpenRouterDispatcher
from .openrouter_quotes import resolve_model_slug
from .operation_pricing import ChargePolicy, UnpricedOperationError
from .package_broker import PackageBroker
from .protected_credentials import (
    MCP_CREDENTIAL_REF_FIELD,
    MCP_CREDENTIAL_REVISION_FIELD,
    MCP_URL_REF_FIELD,
    MCP_URL_REVISION_FIELD,
    SCORER_CREDENTIAL_REF_FIELD,
    SCORER_CREDENTIAL_REVISION_FIELD,
    SCORER_URL_REF_FIELD,
    SCORER_URL_REVISION_FIELD,
)
from .remote_evaluator import RemoteEvaluatorBroker, RemoteEvaluatorTransportError
from .runtime import BudgetRuntime, ProviderFailedError, UsagePendingError
from .signals import BudgetReached
from .usage_tags import USAGE_TAGS_HEADERS

logger = logging.getLogger(__name__)

ROUTE_KEY = "_skynet_budget_route"
# Key, inside a model config's ``extra``, of that model's own economy choice.
ECONOMY_KEY = "economy_mode"
# Key, inside the first optimization model's route, of the embeddings route
# ShinkaEvolve's duplicate rejection uses; it bills like the model beside it.
EMBEDDING_ROUTE_KEY = "embedding"
# Cheap, widely served OpenRouter embeddings model with published endpoint prices.
SHINKA_EMBEDDING_MODEL = "openai/text-embedding-3-small"
# Descriptor key naming the hosts a parent-owned repository scoring box may
# reach; when present, that box is funded alongside the outer sandbox.
PARENT_HOSTS_KEY = "parent_allowed_hosts"
_MAX_REQUEST_BYTES = 32 * 1024 * 1024
_BUILTIN_PATHS = frozenset({"/v1/_sandbox", "/v1/_mcp", "/v1/_evaluator", "/v1/_packages", "/v1/_budget/state"})
# The state read travels the sandbox mailbox relay, which can lag for seconds
# under load; one slow reply must not discard a finished run's result.
_BUDGET_STATE_ATTEMPTS = 3
_BUDGET_STATE_TIMEOUT_SECONDS = 30
_BUDGET_STATE_RETRY_DELAY_SECONDS = 2
_MODEL_ATTEMPT_ID = re.compile(r"[0-9a-f]{32}\Z")
_TRANSIENT_SANDBOX_ERROR_TYPES = frozenset(
    {
        "SandboxResponseError",
        "SandboxStreamError",
        "SandboxTimeoutError",
    }
)


def _unbound_capability(path: str, capabilities: Mapping[str, Any]) -> bool:
    """Return whether ``path`` names a gateway capability that this gateway does not serve.

    Args:
        path: The request path, such as ``/v1/_host_agent``.
        capabilities: The paths bound with ``bind_capability``.

    Returns:
        True for a ``/v1/_`` path that is neither built in nor bound.
    """
    return path.startswith("/v1/_") and path not in _BUILTIN_PATHS and path not in capabilities


def _sandbox_failure_kind(error: BaseException) -> str:
    """Classify trusted sandbox failures from their exception types and causes.

    Args:
        error: Exception raised by the parent-owned sandbox implementation.

    Returns:
        A stable failure kind for the sandbox child protocol.
    """
    pending = [error]
    seen: set[int] = set()
    while pending:
        current = pending.pop()
        if id(current) in seen:
            continue
        seen.add(id(current))
        if isinstance(current, InfrastructureInterruptionError | httpx.TransportError):
            return INFRASTRUCTURE_INTERRUPTION
        if type(current).__name__ in _TRANSIENT_SANDBOX_ERROR_TYPES:
            return INFRASTRUCTURE_INTERRUPTION
        pending.extend(
            nested
            for nested in (current.__cause__, current.__context__, getattr(current, "cause", None))
            if isinstance(nested, BaseException)
        )
    return DETERMINISTIC_FAILURE


def _model_attempt_key(headers: Mapping[str, str]) -> str | None:
    """Validate a supervisor-issued physical model attempt identity.

    Args:
        headers: Normalized request headers crossing the protected relay.

    Returns:
        A durable operation key, or None for trusted direct setup probes.

    Raises:
        UnpricedOperationError: When an alleged mailbox identity is malformed.
    """
    identity = headers.get(MODEL_ATTEMPT_HEADER)
    if identity is None:
        return None
    if _MODEL_ATTEMPT_ID.fullmatch(identity) is None:
        raise UnpricedOperationError("The model attempt identity is invalid.")
    return f"model-attempt:{identity}"


class EvaluatorEndpoint(Protocol):
    """Answer guest scoring requests without importing optimizer code."""

    def dispatch(self, body: Mapping[str, Any]) -> ModelHTTPResult:
        """Score one candidate the guest posted."""
        ...


class SandboxControl(Protocol):
    """Describe the parent-owned sandbox boundary without importing optimizer code."""

    def handle(self, action: str, payload: Mapping[str, Any]) -> dict[str, Any]:
        """Perform a non-streaming action on an owned sandbox."""
        ...

    def run(self, payload: Mapping[str, Any], on_output: Any = None) -> Any:
        """Execute an owned guest command and return its terminal result."""
        ...

    def parent_runtime(self, allowed_hosts: tuple[str, ...]) -> Any:
        """Return a runtime for the parent's own boxes, never reachable by the guest."""
        ...

    def close(self) -> None:
        """Stop every owned sandbox and settle its confirmed usage."""
        ...


def raise_gateway_stop(route: Mapping[str, Any]) -> None:
    """Restore the typed control signal after a native CLI reports a gateway failure.

    Args:
        route: Scoped endpoint and opaque credential supplied by the trusted parent.

    Raises:
        BudgetReached: When the authoritative parent stopped admission for budget.
        UsagePendingError: When the authority is closed for another accounting reason.
    """
    url = f"{app_env('APP_BUDGET_RELAY_URL', str(route['url'])).rstrip('/')}/_budget/state"
    for attempt in range(_BUDGET_STATE_ATTEMPTS):
        try:
            response = httpx.get(
                url,
                headers={"Authorization": f"Bearer {route['token']}"},
                timeout=_BUDGET_STATE_TIMEOUT_SECONDS,
                trust_env=False,
            )
            break
        except httpx.TransportError as error:
            if attempt == _BUDGET_STATE_ATTEMPTS - 1:
                raise InfrastructureInterruptionError("The trusted parent model transport was interrupted.") from error
            time.sleep(_BUDGET_STATE_RETRY_DELAY_SECONDS)
    response.raise_for_status()
    budget = response.json()
    if budget.get("blocked_reason") == "budget_reached":
        raise BudgetReached()
    if budget.get("blocked_reason"):
        raise UsagePendingError("The spending authority is awaiting reconciliation.")


class ModelGateway:
    """Keep upstream credentials and dispatch authority outside the optimizer child."""

    def __init__(
        self,
        runtime: BudgetRuntime,
        *,
        timeout: float = 600,
    ) -> None:
        """Start a loopback-only service for one setup or run authority.

        Args:
            runtime: Owner-bound, generation-fenced spending context.
            timeout: Physical provider request timeout, excluding hidden retries.
        """
        self.runtime = runtime
        # The guest only sees a 402 for a call the budget could not cover, so the
        # parent keeps the refusal to tell a spent budget from a code failure,
        # and its type to tell the limit from the account balance.
        self.budget_refusal: BudgetInsufficientError | None = None
        self._client = httpx.Client(timeout=timeout, follow_redirects=False, trust_env=False)
        self._provider_clients: list[httpx.Client] = []
        self._routes: dict[str, OpenRouterDispatcher | CliDispatcher] = {}
        self._capabilities: dict[str, Callable[[bytes, dict[str, str]], ModelHTTPResult]] = {}
        self._capability_tokens: dict[str, str] = {}
        self._observers: dict[str, list[Callable[[Mapping[str, Any]], None]]] = {}
        self._parent_only: set[str] = set()
        self._sandbox: SandboxControl | None = None
        self._tools: McpToolsBroker | None = None
        self._evaluator: EvaluatorEndpoint | None = None
        self._packages: PackageBroker | None = None
        self._package_token = secrets.token_urlsafe(32)
        self._evaluator_token = secrets.token_urlsafe(32)
        self._tool_token = secrets.token_urlsafe(32)
        self._control_token = secrets.token_urlsafe(32)
        self._descriptor: dict[str, Any] | None = None
        # Set once the job's event queue exists, so an economy batch's polling
        # keeps the stall watchdog from mistaking a long wait for a hang.
        self.heartbeat: Callable[[], None] | None = None
        gateway = self

        class Handler(BaseHTTPRequestHandler):
            """Serve only registered model roles and their own budget state."""

            def log_message(self, format: str, *args: Any) -> None:
                """Avoid logging guest requests or scoped credentials."""

            def _route(self) -> OpenRouterDispatcher | CliDispatcher | None:
                """Resolve a short-lived role token without accepting provider credentials."""
                token = self.headers.get("Authorization", "").removeprefix("Bearer ")
                if not token:
                    token = self.headers.get("x-api-key", "")
                return gateway._routes.get(token)

            def _reply(self, response: ModelHTTPResult) -> None:
                """Write a bounded protocol response with no credential-bearing headers."""
                self.send_response(response.status)
                self.send_header("Content-Type", response.content_type)
                self.send_header("Content-Length", str(len(response.body)))
                self.end_headers()
                self.wfile.write(response.body)

            def _error(self, status: int, code: str, message: str) -> None:
                """Report a machine-readable admission result to every guest protocol."""
                self._reply(
                    ModelHTTPResult(
                        status,
                        "application/json",
                        json.dumps({"error": {"type": code, "code": code, "message": message}}).encode(),
                    )
                )

            def _unpriced(self, error: Exception, route: OpenRouterDispatcher | CliDispatcher | None = None) -> None:
                """Map known route refusals to stable localized error keys.

                Args:
                    error: The route refusal to report.
                    route: Scoped model route, if this request used one.

                Returns:
                    None, after sending the error response.
                """
                message = str(error)
                known = {
                    "submission.byok_route_unsupported",
                    "submission.byok_embeddings_unsupported",
                    "submission.byok_provider_unsupported",
                }
                code = error.code if isinstance(error, UnpricedOperationError) else None
                if route is None or route.policy.kind != "byok_model" or code not in known:
                    code = None
                self._error(422, code or "unpriced_operation", t_en(code) if code else message)

            def do_GET(self) -> None:
                """Return state only to a token already bound to this spending authority."""
                token = self.headers.get("Authorization", "").removeprefix("Bearer ")
                if self._route() is None and not (
                    (gateway._tools is not None and secrets.compare_digest(token, gateway._tool_token))
                    or (gateway._evaluator is not None and secrets.compare_digest(token, gateway._evaluator_token))
                ):
                    self._error(401, "unauthorized", "Unknown scoped model route.")
                    return
                if self.path != "/v1/_budget/state":
                    self._error(404, "not_found", "Unknown protected endpoint.")
                    return
                body = gateway._budget_state()
                self._reply(ModelHTTPResult(200, "application/json", json.dumps(body, default=str).encode()))

            def do_POST(self) -> None:
                """Admit the fully resolved SDK body before forwarding any provider request."""
                if self.path == "/v1/_sandbox":
                    self._sandbox_request()
                    return
                route = self._route()
                if self.path in gateway._capabilities:
                    token = self.headers.get("Authorization", "").removeprefix("Bearer ")
                    if not token:
                        token = self.headers.get("x-api-key", "")
                    self._capability_request(gateway._capability_refusal(self.path, token))
                    return
                token = self.headers.get("Authorization", "").removeprefix("Bearer ")
                is_tools = (
                    self.path == "/v1/_mcp"
                    and gateway._tools is not None
                    and secrets.compare_digest(token, gateway._tool_token)
                )
                is_evaluator = (
                    self.path == "/v1/_evaluator"
                    and gateway._evaluator is not None
                    and secrets.compare_digest(token, gateway._evaluator_token)
                )
                is_packages = (
                    self.path == "/v1/_packages"
                    and gateway._packages is not None
                    and secrets.compare_digest(token, gateway._package_token)
                )
                if route is None and not is_tools and not is_evaluator and not is_packages:
                    self._error(401, "unauthorized", "Unknown scoped model route.")
                    return
                if _unbound_capability(self.path, gateway._capabilities):
                    self._error(404, "not_found", "Unknown protected endpoint.")
                    return
                try:
                    length = int(self.headers.get("Content-Length", "0"))
                    if not 0 < length <= _MAX_REQUEST_BYTES:
                        self._error(413, "request_too_large", "The model request exceeds the supported size.")
                        return
                    body = json.loads(self.rfile.read(length))
                    if not isinstance(body, dict):
                        raise TypeError("A JSON object is required.")
                    response = (
                        gateway._packages.dispatch(body)
                        if is_packages
                        else gateway._evaluator.dispatch(body)
                        if is_evaluator
                        else gateway._tools.dispatch(body)
                        if is_tools
                        else route.dispatch(
                            self.path.removeprefix("/v1"),
                            body,
                            operation_key=_model_attempt_key(
                                {name.lower(): value for name, value in self.headers.items()}
                            ),
                            protocol_headers={
                                name: value
                                for name, value in self.headers.items()
                                if name.lower() in {"anthropic-version", "anthropic-beta"} | USAGE_TAGS_HEADERS
                            },
                        )
                    )
                    self._reply(response)
                    if route is not None and not (is_tools or is_evaluator or is_packages):
                        gateway._observe(route, body, response)
                except BudgetReached:
                    self._error(402, "budget_reached", "The remaining budget cannot cover the next operation.")
                except BudgetInsufficientError:
                    self._error(402, "budget_insufficient", "Increase the total budget or add funds to continue.")
                except ProviderFailedError as error:
                    # Only this attempt's bill is open; a 5xx lets the harness retry with a fresh attempt.
                    self._error(502, "provider_failed", str(error))
                except (UsagePendingError, BudgetError):
                    self._error(424, "usage_pending", "Previous work must settle before another attempt is admitted.")
                except (UnpricedOperationError, ValueError, TypeError) as error:
                    self._unpriced(error, route if not (is_tools or is_evaluator or is_packages) else None)
                except httpx.HTTPError:
                    self._error(
                        502,
                        "provider_transport_interruption",
                        "The model provider transport could not complete this request.",
                    )
                except RemoteEvaluatorTransportError:
                    self._error(
                        502, "evaluator_unavailable", "The configured evaluator could not complete this request."
                    )

            def _capability_request(self, refusal: ModelHTTPResult | None) -> None:
                """Pass a bound capability's raw request to its parent-side handler.

                Args:
                    refusal: The answer for a caller without the capability's
                        token, or ``None`` when the caller may use it.
                """
                if refusal is not None:
                    self._reply(refusal)
                    return
                try:
                    length = int(self.headers.get("Content-Length", "0"))
                    if not 0 < length <= _MAX_REQUEST_BYTES:
                        self._error(413, "request_too_large", "The request exceeds the supported size.")
                        return
                    body = self.rfile.read(length)
                    headers = {name.lower(): value for name, value in self.headers.items()}
                    self._reply(gateway._capabilities[self.path](body, headers))
                except BudgetReached:
                    self._error(402, "budget_reached", "The remaining budget cannot cover the next operation.")
                except (UsagePendingError, BudgetError):
                    self._error(424, "usage_pending", "Previous work must settle before another attempt is admitted.")
                except (UnpricedOperationError, ValueError, TypeError) as error:
                    self._unpriced(error)

            def _sandbox_request(self) -> None:
                """Serve guest control with a separate capability and streamed command output."""
                token = self.headers.get("Authorization", "").removeprefix("Bearer ")
                if not secrets.compare_digest(token, gateway._control_token) or gateway._sandbox is None:
                    self._error(401, "unauthorized", "Unknown sandbox control capability.")
                    return
                streaming = False
                write_lock = threading.Lock()

                def frame(value: dict[str, Any]) -> None:
                    """Flush one NDJSON frame without interleaving parallel output callbacks."""
                    with write_lock:
                        self.wfile.write(json.dumps(value).encode() + b"\n")
                        self.wfile.flush()

                def output(stream: str, data: str) -> None:
                    """Forward ordinary guest output after trusted model interception."""
                    frame({"type": "output", "stream": stream, "data": data})

                try:
                    length = int(self.headers.get("Content-Length", "0"))
                    if not 0 < length <= _MAX_REQUEST_BYTES:
                        raise ValueError("Sandbox request exceeds the supported size.")
                    body = json.loads(self.rfile.read(length))
                    if not isinstance(body, dict) or not isinstance(body.get("payload"), dict):
                        raise TypeError("Sandbox control requires a JSON action and payload.")
                    action, payload = body.get("action"), body["payload"]
                    if action == "run":
                        self.send_response(200)
                        self.send_header("Content-Type", "application/x-ndjson")
                        self.end_headers()
                        streaming = True
                        result = gateway._sandbox.run(payload, on_output=output)
                        frame({"type": "result", **asdict(result)})
                    else:
                        value = gateway._sandbox.handle(action, payload)
                        self._reply(ModelHTTPResult(200, "application/json", json.dumps(value).encode()))
                except BaseException as error:
                    value = {
                        "type": "error",
                        "error_type": type(error).__name__,
                        "failure_kind": _sandbox_failure_kind(error),
                        "message": str(error),
                    }
                    if streaming:
                        frame(value)
                    else:
                        self._reply(ModelHTTPResult(422, "application/json", json.dumps(value).encode()))

        self._server = ThreadingHTTPServer(("127.0.0.1", 0), Handler)
        self._server.daemon_threads = False
        self._thread = threading.Thread(target=self._server.serve_forever, daemon=True, name="budget-model-gateway")
        self._thread.start()

    @property
    def url(self) -> str:
        """Return the loopback endpoint passed to the authorized optimizer child."""
        return f"http://127.0.0.1:{self._server.server_port}/v1"

    def register(
        self,
        *,
        model: str,
        api_key: str,
        role: str,
        policy: ChargePolicy,
        batch: bool = False,
        embeddings: bool = False,
        parent_only: bool = False,
        upstream: ProviderUpstream = OPENROUTER,
    ) -> dict[str, str]:
        """Register one fixed role without exposing its provider credential.

        Args:
            model: Exact permitted OpenRouter model slug.
            api_key: Provider credential retained only in the trusted parent.
            role: Task, judge, or optimization usage attribution.
            policy: Approved conversion policy for this particular model source.
            batch: Send this role's chat calls through half-price OpenRouter batches.
            embeddings: Serve only ``/embeddings`` for an embeddings model instead of chat.
            parent_only: Only the parent calls this route, so readiness probes skip it.
            upstream: Fixed provider endpoint and permitted protocols.

        Returns:
            Scoped guest route containing no upstream credential.
        """
        if embeddings and not upstream.embeddings:
            raise UnpricedOperationError(
                "This provider does not support embeddings on this route.",
                code="submission.byok_embeddings_unsupported",
            )
        if upstream == OPENROUTER:
            model = resolve_model_slug(model, client=self._client)
        elif byok_model_prefix(model, upstream.slug):
            model = model.split("/", 1)[-1]
        client = self._client
        if upstream.slug == "custom":
            try:
                validate_custom_base(upstream.base_url)
                address = _pinned_address(upstream.base_url, False, label="custom provider endpoint")
            except ValueError as error:
                raise UnpricedOperationError(str(error)) from error
            client = httpx.Client(
                transport=_PinnedTransport(upstream.base_url, address),
                timeout=self._client.timeout,
                follow_redirects=False,
                trust_env=False,
            )
            self._provider_clients.append(client)
        token = secrets.token_urlsafe(32)
        # BYOK traffic follows the selected provider's own data policy.
        data_policy = (
            data_policy_for(self.runtime.username)
            if policy.kind == "managed_model" and upstream == OPENROUTER
            else None
        )
        self._routes[token] = OpenRouterDispatcher(
            self.runtime,
            api_key=api_key,
            model=model,
            role=role,
            policy=policy,
            client=client,
            data_policy=data_policy,
            batch=(
                BatchCollector(api_key=api_key, model=model, client=self._client, heartbeat=self._beat)
                if batch and upstream == OPENROUTER
                else None
            ),
            embeddings=embeddings,
            upstream=upstream,
        )
        if parent_only:
            self._parent_only.add(token)
        return {"url": self.url, "token": token, "model": model, "role": role}

    def register_subscription(self, model: str, cli: CliProvider, role: str) -> dict[str, str]:
        """Register one fixed role answered by a local CLI subscription.

        Args:
            model: OpenRouter catalog slug the guest names.
            cli: The CLI that serves the role first.
            role: Task, judge, or optimization usage attribution.

        Returns:
            Scoped guest route containing no credential.

        Raises:
            UnpricedOperationError: Outside local mode, or when ``cli`` cannot run ``model``.
        """
        token = secrets.token_urlsafe(32)
        self._routes[token] = CliDispatcher(self.runtime, cli, model, role, app_config.settings)
        return {"url": self.url, "token": token, "model": model, "role": role}

    def bind_capability(
        self,
        path: str,
        handler: Callable[[bytes, dict[str, str]], ModelHTTPResult],
        *,
        token: str | None = None,
    ) -> None:
        """Serve a parent-side endpoint to callers that hold its token.

        Args:
            path: Gateway path such as ``/v1/_host_agent``.
            handler: Takes the raw request body and lower-cased headers.
            token: The only bearer that reaches the handler; a model route token
                then gets 403. ``None`` admits any registered route token. The
                token is not a model route: model paths still answer 401 for it.

        Raises:
            ValueError: When ``path`` is already bound or would shadow a built-in endpoint.
        """
        if path in self._capabilities or path in _BUILTIN_PATHS:
            raise ValueError(f"The gateway path {path!r} is already bound.")
        self._capabilities[path] = handler
        if token is not None:
            self._capability_tokens[path] = token

    def _capability_refusal(self, path: str, token: str) -> ModelHTTPResult | None:
        """Return the refusal for a caller of a bound capability, or ``None`` to admit it.

        Args:
            path: A bound capability path.
            token: The caller's bearer token.

        Returns:
            ``None`` for the capability's own token (or, without one, any
            registered route token); otherwise a 403 or 401 error response.
        """
        dedicated = self._capability_tokens.get(path)
        if dedicated is not None:
            # Bytes, because compare_digest refuses non-ASCII str from a hostile header.
            if secrets.compare_digest(token.encode(), dedicated.encode()):
                return None
            code, status, message = "forbidden", 403, "This capability needs its own token."
        elif token in self._routes:
            return None
        else:
            code, status, message = "unauthorized", 401, "Unknown scoped model route."
        error = {"error": {"type": code, "code": code, "message": message}}
        return ModelHTTPResult(status, "application/json", json.dumps(error).encode())

    def _beat(self) -> None:
        """Report that an economy batch is still being awaited."""
        if self.heartbeat is not None:
            self.heartbeat()

    def batches(self, token: str) -> bool:
        """Tell whether a route's chat calls wait on half-price batches.

        Args:
            token: The route token.

        Returns:
            True for an economy route.
        """
        return getattr(self._routes.get(token), "_batch", None) is not None

    def model_routes(self) -> list[dict[str, str]]:
        """Return only capabilities registered by this trusted parent for readiness probes."""
        # Callers send chat requests to these routes; an embeddings route
        # refuses them, which would fail every novelty run's setup.
        return [
            {"url": self.url, "token": token, "model": route.model, "role": route.role}
            for token, route in self._routes.items()
            if not route.embeddings and token not in self._parent_only
        ]

    def bind_evaluator(self, evaluator: EvaluatorEndpoint) -> dict[str, str]:
        """Serve the guest's scoring requests from a parent-owned evaluator.

        Args:
            evaluator: Scores each candidate the guest posts, in the parent.

        Returns:
            The opaque route the guest posts its candidates to.

        Raises:
            ValueError: When the gateway already owns an evaluator endpoint.
        """
        if self._evaluator is not None:
            raise ValueError("This gateway already owns an evaluator endpoint.")
        self._evaluator = evaluator
        return {"url": self.url, "token": self._evaluator_token}

    def bind_sandbox(
        self, broker: SandboxControl, *, image: str, lifetime_seconds: float, allowed_hosts: tuple[str, ...] = ()
    ) -> None:
        """Bind the trusted runtime before passing a scoped descriptor to its child.

        Args:
            broker: Parent-owned sandbox credentials and cost accounting.
            image: Immutable deployment profile used for this workload.
            lifetime_seconds: Maximum already bounded by the configured runtime profile.
            allowed_hosts: Hosts the broker lets this run's boxes reach, which
                recovery must fund too.
        """
        if self._sandbox is not None:
            raise ValueError("This gateway already owns a sandbox broker.")
        self._sandbox = broker
        self._descriptor = {
            "url": self.url,
            "control_token": self._control_token,
            "image": image,
            "lifetime_seconds": lifetime_seconds,
            **({"allowed_hosts": sorted(allowed_hosts)} if allowed_hosts else {}),
        }

    def fund_parent_sandbox(self, allowed_hosts: tuple[str, ...]) -> None:
        """Count one more parent-owned box reaching ``allowed_hosts`` in this run's recovery bound.

        Args:
            allowed_hosts: Hosts the parent's box may reach before it goes offline.

        Raises:
            ValueError: When no sandbox broker is bound yet.
        """
        if self._descriptor is None:
            raise ValueError("This gateway has no sandbox broker to open a parent box with.")
        self._descriptor[PARENT_HOSTS_KEY] = sorted(set(allowed_hosts))

    def parent_sandbox_runtime(self) -> Any:
        """Return the runtime the parent opens its own scoring box with.

        Returns:
            A runtime limited to the hosts :meth:`fund_parent_sandbox` recorded.

        Raises:
            ValueError: When the parent box was never funded.
        """
        hosts = (self._descriptor or {}).get(PARENT_HOSTS_KEY)
        if self._sandbox is None or hosts is None:
            raise ValueError("Fund the parent's box before opening it.")
        return self._sandbox.parent_runtime(tuple(hosts))

    def dispatch_guest(
        self, token: str, path: str, body: Mapping[str, Any], headers: Mapping[str, str]
    ) -> ModelHTTPResult:
        """Serve a sandbox mailbox request under its original fixed model role.

        Args:
            token: Short-lived scoped role credential registered by this parent.
            path: Supported model protocol path or own-budget state endpoint.
            body: Exact model request emitted by the guest SDK.
            headers: Permitted protocol headers, never upstream credentials.

        Returns:
            Model response after authoritative admission and settlement.
        """
        route = self._routes.get(token)
        if path in self._capabilities:
            refusal = self._capability_refusal(path, token)
            if refusal is not None and path not in self._capability_tokens:
                raise ValueError("Unknown scoped model route.")
            if refusal is not None:
                return refusal
            body_bytes = json.dumps(dict(body)).encode()
            return self._capabilities[path](body_bytes, {name.lower(): value for name, value in headers.items()})
        is_tools = self._tools is not None and secrets.compare_digest(token, self._tool_token)
        is_evaluator = self._evaluator is not None and secrets.compare_digest(token, self._evaluator_token)
        is_packages = self._packages is not None and secrets.compare_digest(token, self._package_token)
        if route is None and not is_tools and not is_evaluator and not is_packages:
            raise ValueError("Unknown scoped model route.")
        if _unbound_capability(path, self._capabilities):
            message = {"error": {"type": "not_found", "code": "not_found", "message": "Unknown protected endpoint."}}
            return ModelHTTPResult(404, "application/json", json.dumps(message).encode())
        if path == "/v1/_budget/state":
            snapshot = self._budget_state()
            return ModelHTTPResult(200, "application/json", json.dumps(snapshot, default=str).encode())
        if is_packages:
            if path != "/v1/_packages":
                raise ValueError("The package capability can only fetch its registered artifacts.")
            return self._packages.dispatch(body)
        if is_evaluator:
            if path != "/v1/_evaluator":
                raise ValueError("The evaluator capability cannot dispatch models, tools, or sandbox commands.")
            return self._evaluator.dispatch(body)
        if is_tools:
            if path != "/v1/_mcp":
                raise ValueError("The tool capability cannot dispatch models or sandbox commands.")
            return self._tools.dispatch(body)
        try:
            response = route.dispatch(
                path.removeprefix("/v1"),
                body,
                operation_key=_model_attempt_key(headers),
                protocol_headers=headers,
            )
        except BudgetInsufficientError as error:
            self.budget_refusal = error
            raise
        self._observe(route, body, response)
        return response

    def observe_role(self, role: str, observer: Callable[[Mapping[str, Any]], None]) -> None:
        """Show every answered model call of one role to a parent-side observer.

        Args:
            role: The route role to watch, such as ``optimization``.
            observer: Receives each answered request body; it must return quickly.
        """
        self._observers.setdefault(role, []).append(observer)

    def _observe(
        self, route: OpenRouterDispatcher | CliDispatcher, body: Mapping[str, Any], response: ModelHTTPResult
    ) -> None:
        """Pass one answered model call to its role's observers.

        Args:
            route: The route that served the call.
            body: The model request.
            response: The reply the caller received.
        """
        if not 200 <= response.status < 300:
            return
        for observer in self._observers.get(route.role, ()):
            try:
                observer(body)
            except Exception:  # isolation boundary: an observer must never fail the model call it watched
                logger.warning("A model-call observer failed.", exc_info=True)

    def _budget_state(self) -> dict[str, Any]:
        """Expose only the owning run's state and mark replaced capabilities as fenced."""
        snapshot = asdict(self.runtime.service.get(self.runtime.budget_id, self.runtime.username))
        snapshot.pop("username", None)
        snapshot.pop("account_available_cents", None)
        if snapshot["generation"] != self.runtime.generation:
            snapshot["blocked_reason"] = "generation_fenced"
        return snapshot

    def cost_ceiling_cents(self) -> int:
        """Return the cents a guest may plan its own spending against.

        Returns:
            The budget's total, or for a budget without a limit whatever the
            account can still fund; never below one cent so the guest's
            ceiling stays a valid request field.
        """
        snapshot = self.runtime.service.get(self.runtime.budget_id, self.runtime.username)
        ceiling = snapshot.available_cents if snapshot.uncapped else snapshot.total_cents
        return max(1, int(ceiling))

    def protect_payload(
        self,
        payload: dict[str, Any],
        *,
        managed_key: str,
        allow_private_tools: bool = False,
        authorize_tool: Callable[[str, str, dict[str, Any]], bool] | None = None,
        on_tool_event: Callable[[dict[str, Any]], None] | None = None,
        allow_batch: bool = True,
    ) -> dict[str, Any]:
        """Replace resolved provider credentials with fixed, metered model-role routes.

        Args:
            payload: In-memory submission after account-owned credentials are resolved.
            managed_key: Platform OpenRouter credential used only for managed roles.
            allow_private_tools: Explicit deployment opt-in for private MCP destinations.
            authorize_tool: Optional blocking approval gate for protected chat tools.
            on_tool_event: Optional lifecycle event receiver for protected chat tools.
            allow_batch: Whether economy choices may route managed models through batches.

        Returns:
            A copied payload carrying opaque role routes and no model provider keys.

        Raises:
            UnpricedOperationError: When a configured external route cannot be verified.
        """
        result = copy.deepcopy(payload)
        for key in (
            "_skynet_target_route",
            "_skynet_tools_route",
            "_skynet_evaluator_route",
            "_skynet_packages_route",
            "_budget_gateway_descriptor",
        ):
            result.pop(key, None)
        source = result.get("tool_source")
        if isinstance(source, dict) and source.get("kind") == "live_mcp":
            if any(
                field in source
                for field in (
                    MCP_CREDENTIAL_REF_FIELD,
                    MCP_CREDENTIAL_REVISION_FIELD,
                    MCP_URL_REF_FIELD,
                    MCP_URL_REVISION_FIELD,
                )
            ):
                raise ValueError("The MCP credential reference must be resolved by the trusted parent.")
            if self._tools is not None:
                raise ValueError("This gateway already owns a tool roster.")
            self._tools = McpToolsBroker(
                str(source.get("mcp_url") or ""),
                auth_header=source.pop("mcp_auth_header", None),
                tool_filter=source.get("tool_filter"),
                allow_private=allow_private_tools,
                check_admission=self.runtime.check_admission,
                authorize_tool=authorize_tool,
                on_tool_event=on_tool_event,
            )
            source["mcp_url"] = "https://scoped-tools.invalid/mcp"
            result["_skynet_tools_route"] = {"url": self.url, "token": self._tool_token}
        if self._descriptor is not None:
            result["_budget_gateway_descriptor"] = {
                key: value for key, value in self._descriptor.items() if key != PARENT_HOSTS_KEY
            }
        target = result.get("target")
        target_config = result.get("task_model_config") if isinstance(target, dict) else None
        target_route: dict[str, str] | None = None
        models = []
        for key, role in (
            ("model_config", "task"),
            ("reflection_model_config", "optimization"),
            ("task_model_config", "task"),
        ):
            if isinstance(result.get(key), dict):
                models.append((result[key], role))
        scorer = result.get("scorer")
        if isinstance(scorer, dict) and scorer.get("kind", "python") == "python":
            lock_document = scorer.get("dependency_lock")
            registry = result.pop("dependency_registry", None)
            if lock_document is not None:
                if self._descriptor is None:
                    raise ValueError("Package installation requires the protected sandbox profile.")
                lock = verify_dependency_lock(
                    lock_document,
                    image=self._descriptor["image"],
                    code=str(scorer.get("metric_code") or ""),
                )
                registry = lock.registry_url
                artifacts = [wheel.model_dump(mode="json") for wheel in lock.artifacts]
            else:
                artifacts = None
            if registry is not None:
                self._packages = PackageBroker(
                    registry,
                    check_admission=self.runtime.check_admission,
                    allow_private=allow_private_tools,
                    artifacts=artifacts,
                )
                result["_skynet_packages_route"] = {"url": self.url, "token": self._package_token}
        if isinstance(scorer, dict) and scorer.get("kind") == "remote":
            if any(
                field in scorer
                for field in (
                    SCORER_CREDENTIAL_REF_FIELD,
                    SCORER_CREDENTIAL_REVISION_FIELD,
                    SCORER_URL_REF_FIELD,
                    SCORER_URL_REVISION_FIELD,
                )
            ):
                raise ValueError("The evaluator credential reference must be resolved by the trusted parent.")
            if self._evaluator is not None:
                raise ValueError("This gateway already owns an evaluator endpoint.")
            self._evaluator = RemoteEvaluatorBroker(
                str(scorer.get("url") or ""),
                secret=scorer.pop("secret", None),
                timeout_seconds=float(scorer.get("timeout_seconds", 60)),
                check_admission=self.runtime.check_admission,
                allow_private=allow_private_tools,
            )
            scorer["url"] = "https://scoped-evaluator.invalid/score"
            # Remote evaluators never use the optional Python scorer model or its credentials.
            scorer.pop("model", None)
            result["_skynet_evaluator_route"] = {"url": self.url, "token": self._evaluator_token}
        elif isinstance(scorer, dict) and isinstance(scorer.get("model"), dict):
            models.append((scorer["model"], "judge"))
        for key, role in (
            ("generation_models", "task"),
            ("reflection_models", "optimization"),
            ("extra_reflection_model_configs", "optimization"),
        ):
            models.extend((config, role) for config in result.get(key, []) if isinstance(config, dict))
        economy = bool(result.get("economy_mode"))
        shinka = result.get("shinka")
        novelty = isinstance(shinka, dict) and bool(shinka.get("novelty"))
        reflection = result.get("reflection_model_config")
        if isinstance(reflection, dict):
            validate_byok_novelty(
                {**reflection, "token_source": reflection.get("token_source") or result.get("token_source")}, novelty
            )
        for config, role in models:
            source = config.get("token_source") or result.get("token_source") or "managed"
            extra = config.get("extra") or {}
            # A model's own economy choice rides in ``extra`` so the sandbox
            # request shape stays fixed. Batching is decided here, so the flag
            # never reaches the guest, whose model calls pass ``extra`` on.
            economy_model = bool(extra.pop(ECONOMY_KEY, False))
            if source == "subscription":
                route = self._register_subscription_config(config, role)
                if config is target_config:
                    target_name = str(target.get("model") or "").strip("/")
                    if target_name not in {str(config.get("name") or "").strip("/"), route["model"]}:
                        raise UnpricedOperationError(
                            "The agent task model does not match its credential configuration."
                        )
                    target_route = route
                cleaned = scrub_model_config(config)
                config.clear()
                config.update(cleaned)
                config["extra"] = {**dict(config.get("extra") or {}), ROUTE_KEY: route}
                config.pop("base_url", None)
                continue
            endpoint = extra.get("api_base") or extra.get("base_url") or config.get("base_url")
            upstream = resolve_upstream(config) if source == "byok" else OPENROUTER
            if source == "managed" and endpoint and urlsplit(str(endpoint)).hostname != "openrouter.ai":
                raise UnpricedOperationError("An explicit custom endpoint cannot be silently rerouted.")
            model = str(extra.get("model") or config.get("name", "")).removeprefix("openrouter/")
            key = extra.get("api_key") if source == "byok" else managed_key
            if not key:
                raise UnpricedOperationError("This model role has no available provider credential.")
            # The run-level flag is the older form and still means every model.
            batched = allow_batch and (economy or economy_model) and source == "managed"
            route = self.register(
                model=model,
                api_key=str(key),
                role=role,
                policy=ChargePolicy("byok_model" if source == "byok" else "managed_model"),
                batch=batched,
                upstream=upstream,
            )
            if config is target_config:
                target_name = str(target.get("model") or "").strip("/")
                if not target_name or target_name != str(config.get("name") or "").strip("/"):
                    raise UnpricedOperationError("The agent task model does not match its credential configuration.")
                target_route = route
            cleaned = scrub_model_config(config)
            config.clear()
            config.update(cleaned)
            config["extra"] = dict(config.get("extra") or {})
            if novelty and config is result.get("reflection_model_config"):
                route = {
                    **route,
                    EMBEDDING_ROUTE_KEY: self.register(
                        model=SHINKA_EMBEDDING_MODEL,
                        api_key=str(key),
                        role=role,
                        policy=ChargePolicy("byok_model" if source == "byok" else "managed_model"),
                        embeddings=True,
                        upstream=upstream,
                    ),
                }
            config["extra"][ROUTE_KEY] = route
            if batched:
                # A batch can outlast any per-call timeout; a guest that gave up
                # would retry as a second paid request while the first still runs.
                config["extra"]["timeout"] = VERCEL_SANDBOX_LIFETIME_CEILING_SECONDS
            config.pop("base_url", None)
        if isinstance(target, dict) and target.get("model"):
            if target_route is not None:
                result["_skynet_target_route"] = target_route
            else:
                if result.get("token_source") == "byok":
                    raise UnpricedOperationError("A BYOK agent target needs an explicit verified model configuration.")
                if result.get("token_source") == "subscription":
                    raise UnpricedOperationError("A subscription agent target needs an explicit model configuration.")
                result["_skynet_target_route"] = self.register(
                    model=str(target["model"]).removeprefix("openrouter/"),
                    api_key=managed_key,
                    role="task",
                    policy=ChargePolicy("managed_model"),
                )
        return result

    def _register_subscription_config(self, config: Mapping[str, Any], role: str) -> dict[str, str]:
        """Register the subscription route a ``token_source: subscription`` model config names.

        Args:
            config: The model config; its name is a catalog slug or ``subscription/<cli>/<slug>``.
            role: Usage attribution.

        Returns:
            The scoped route.

        Raises:
            UnpricedOperationError: Outside local mode, or without a CLI that can run the model.
        """
        if not app_config.settings.local_mode:
            raise UnpricedOperationError("Subscription models run only in local mode.")
        name = str(config.get("name") or "").removeprefix("openrouter/")
        parsed = parse_subscription_model_id(name)
        cli, model = parsed if parsed is not None else (config.get("cli_provider"), name)
        if cli not in ("codex", "claude") or not model:
            raise UnpricedOperationError("A subscription model needs a CLI that can run it.")
        return self.register_subscription(model, cli, role)

    def close(self) -> None:
        """Finish covered requests before releasing the trusted transport and route tokens."""
        for route in self._routes.values():
            route.stop_batch()
        try:
            self._server.shutdown()
            self._server.server_close()
            self._thread.join(timeout=2)
            if self._sandbox is not None:
                self._sandbox.close()
        finally:
            self._client.close()
            for client in self._provider_clients:
                client.close()
            self._provider_clients.clear()
            self._routes.clear()
            self._capabilities.clear()
            self._tools = None
            self._evaluator = None
            self._packages = None
