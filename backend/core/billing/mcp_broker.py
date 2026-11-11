"""Proxy one selected MCP roster without giving authored code network credentials."""

from __future__ import annotations

import asyncio
import ipaddress
import json
import socket
import threading
import uuid
from collections.abc import Callable, Mapping
from typing import Any
from urllib.parse import urlsplit

import httpx
from mcp import ClientSession, types
from mcp.client.streamable_http import streamable_http_client

from .budgets import BudgetError
from .credential_safety import credential_fragments, redact_secret_value
from .model_dispatch import ModelHTTPResult
from .runtime import UsagePendingError
from .signals import BudgetReached

_METADATA_ADDRESSES = frozenset({ipaddress.ip_address("169.254.169.254"), ipaddress.ip_address("fd00:ec2::254")})


class EndpointResolutionError(ValueError):
    """Identify temporary endpoint DNS resolution failures."""


def _pinned_address(url: str, allow_private: bool, *, label: str = "MCP endpoint") -> str:
    """Apply endpoint-discovery policy once and pin its permitted destination address.

    Args:
        url: User-selected MCP endpoint retained by the trusted parent.
        allow_private: Existing deployment opt-in for private endpoint discovery.
        label: Endpoint category used in safe validation messages.

    Returns:
        A verified IP literal used for every connection to this endpoint.

    Raises:
        EndpointResolutionError: When DNS cannot resolve the hostname.
        ValueError: When the endpoint or resolved address is unsafe.
    """
    parsed = urlsplit(url)
    if parsed.scheme not in {"http", "https"} or not parsed.hostname or parsed.username or parsed.password:
        raise ValueError(f"The {label} must be an HTTP or HTTPS URL without embedded credentials.")
    if parsed.fragment:
        raise ValueError(f"The {label} cannot contain a URL fragment.")
    try:
        infos = socket.getaddrinfo(parsed.hostname, parsed.port, type=socket.SOCK_STREAM)
    except (socket.gaierror, ValueError) as error:
        raise EndpointResolutionError(f"The {label} hostname could not be resolved.") from error
    if not infos:
        raise EndpointResolutionError(f"The {label} hostname has no resolved addresses.")
    for info in infos:
        address = ipaddress.ip_address(info[4][0])
        if address in _METADATA_ADDRESSES:
            raise ValueError(f"The {label} resolves to a blocked metadata-service address.")
        if not allow_private and not address.is_global:
            raise ValueError(f"This deployment does not permit a private {label}.")
    return str(infos[0][4][0])


class _PinnedTransport(httpx.AsyncBaseTransport, httpx.BaseTransport):
    """Keep DNS resolution and redirects from expanding the selected MCP destination."""

    def __init__(self, url: str, address: str) -> None:
        """Bind the verified origin and a transport with no connection retries."""
        self.origin = httpx.URL(url)
        self.address = address
        self.transport = httpx.AsyncHTTPTransport(retries=0, trust_env=False)
        self.sync_transport: httpx.HTTPTransport | None = None

    def _pin_request(self, request: httpx.Request) -> httpx.Request:
        """Retain the original authority while selecting the verified address.

        Args:
            request: Request addressed to the registered origin.

        Returns:
            A request addressed to the pinned IP with the original Host and SNI.

        Raises:
            ValueError: When the request targets a different origin.
        """
        if (request.url.scheme, request.url.host, request.url.port) != (
            self.origin.scheme,
            self.origin.host,
            self.origin.port,
        ):
            raise ValueError("The MCP connection attempted to leave its registered origin.")
        return httpx.Request(
            request.method,
            request.url.copy_with(host=self.address),
            headers=request.headers,
            stream=request.stream,
            extensions={**request.extensions, "sni_hostname": self.origin.host},
        )

    async def handle_async_request(self, request: httpx.Request) -> httpx.Response:
        """Dial only the pinned address while verifying TLS against its original hostname."""
        return await self.transport.handle_async_request(self._pin_request(request))

    def handle_request(self, request: httpx.Request) -> httpx.Response:
        """Use the same origin and TLS pin for synchronous model requests.

        Args:
            request: Request addressed to the registered origin.

        Returns:
            The upstream response.

        Raises:
            ValueError: When the request targets a different origin.
        """
        if self.sync_transport is None:
            self.sync_transport = httpx.HTTPTransport(retries=0, trust_env=False)
        return self.sync_transport.handle_request(self._pin_request(request))

    def close(self) -> None:
        """Release synchronous connections to the pinned endpoint."""
        if self.sync_transport is not None:
            self.sync_transport.close()

    async def aclose(self) -> None:
        """Release the selected endpoint's connection resources."""
        await self.transport.aclose()
        self.close()


class ToolCall:
    """Run one metered tool request and record whether it reached the provider."""

    def __init__(self, run: Callable[[ToolCall], dict[str, Any]]) -> None:
        """Bind the physical request that receives this call's progress record.

        Args:
            run: The broker request; it sets ``sent`` just before the remote tool call.
        """
        self.run = run
        self.sent = False

    def __call__(self) -> dict[str, Any]:
        """Run the request once.

        Returns:
            The provider's tool result.
        """
        return self.run(self)


class McpToolsBroker:
    """Expose only one parent-authorized MCP roster through a scoped guest capability."""

    def __init__(
        self,
        url: str,
        *,
        check_admission: Callable[[], None],
        auth_header: str | None = None,
        tool_filter: list[str] | None = None,
        allow_private: bool = False,
        timeout_seconds: float = 30,
        authorize_tool: Callable[[str, str, dict[str, Any]], bool] | None = None,
        headers: Mapping[str, str] | None = None,
        meter: Callable[[str, Mapping[str, Any], ToolCall], dict[str, Any]] | None = None,
        token_source: Callable[[], str] | None = None,
    ) -> None:
        """Retain endpoint credentials and resolve a permitted destination outside the guest.

        Args:
            url: Fixed MCP endpoint selected by the authenticated owner.
            check_admission: Parent generation and cancellation guard before physical work.
            auth_header: Optional endpoint credential never passed to authored code.
            tool_filter: Selected tool names, or None for the endpoint's exposed roster.
            allow_private: Deployment's existing private-discovery policy.
            timeout_seconds: Per-request timeout without tool-call retries.
            authorize_tool: Optional blocking approval gate for a live chat call.
            headers: Optional parent-held credential headers, such as a provider API key header.
            meter: Optional hook that pays for one tool call; it gets the tool name, the
                arguments and the physical call, and returns the call's result. The call's
                ``sent`` flag tells the hook whether the provider could have run the tool.
            token_source: Optional parent-side source of a current OAuth access token. The
                broker asks it before each request, so a refreshed token takes effect at
                once, and adds it as a bearer header that the guest never sees.
        """
        self.url = url
        self.address = _pinned_address(url, allow_private)
        merged = {**({"Authorization": auth_header} if auth_header else {}), **dict(headers or {})}
        self.headers = merged or None
        fragments = set(credential_fragments(auth_header, endpoint=url))
        for value in (headers or {}).values():
            fragments.update(credential_fragments(value))
        self._redactions = tuple(sorted(fragments, key=len, reverse=True))
        self.meter = meter
        self.token_source = token_source
        self.tool_filter = tuple(tool_filter) if tool_filter else None
        self.check_admission = check_admission
        self.timeout_seconds = timeout_seconds
        self.authorize_tool = authorize_tool
        self._tool_names: set[str] | None = None
        self._roster_lock = threading.Lock()

    def _client_factory(
        self,
        headers: dict[str, str] | None = None,
        timeout: httpx.Timeout | None = None,
        auth: httpx.Auth | None = None,
    ) -> httpx.AsyncClient:
        """Create the MCP SDK transport with fixed-origin routing and redirects disabled."""
        return httpx.AsyncClient(
            headers=headers,
            timeout=timeout or self.timeout_seconds,
            auth=auth,
            follow_redirects=False,
            trust_env=False,
            transport=_PinnedTransport(self.url, self.address),
        )

    def _credentials(self) -> tuple[dict[str, str] | None, tuple[str, ...]]:
        """Return the headers and redactions for one request, with a current OAuth token.

        Returns:
            The request headers and the secret fragments to redact from its result.
        """
        if self.token_source is None:
            return self.headers, self._redactions
        token = self.token_source()
        headers = {**(self.headers or {}), "Authorization": f"Bearer {token}"}
        fragments = set(self._redactions) | set(credential_fragments(token))
        return headers, tuple(sorted(fragments, key=len, reverse=True))

    async def _request(
        self,
        method: str,
        params: Mapping[str, Any],
        call: ToolCall | None = None,
        credentials: tuple[dict[str, str] | None, tuple[str, ...]] | None = None,
    ) -> dict[str, Any]:
        """Execute an original MCP operation over the same fresh-session semantics as ReAct.

        Args:
            method: Selected remote MCP operation.
            params: Original call arguments with its already authorized tool name.
            call: Optional metered call whose ``sent`` flag marks the remote tool request.
            credentials: Request headers and redactions; defaults to the fixed ones.

        Returns:
            The redacted protocol result.
        """
        headers, redactions = credentials or (self.headers, self._redactions)
        self.check_admission()
        client = self._client_factory(
            headers=headers, timeout=httpx.Timeout(self.timeout_seconds, read=self.timeout_seconds)
        )
        async with (
            client,
            streamable_http_client(self.url, http_client=client) as (read, write, _),
            ClientSession(read, write) as session,
        ):
            await session.initialize()
            self.check_admission()
            if method == "tools/list":
                result = await session.list_tools()
            else:
                if call is not None:
                    call.sent = True
                result = await session.call_tool(params["name"], arguments=dict(params.get("arguments") or {}))
            return redact_secret_value(
                result.model_dump(mode="json", by_alias=True, exclude_none=True),
                redactions,
            )

    def _list(self) -> dict[str, Any]:
        """Record exactly the exposed roster and enforce the owner's optional name filter."""
        with self._roster_lock:
            result = self._run_request("tools/list", {})
            available = {tool["name"]: tool for tool in result["tools"]}
            if self.tool_filter is not None:
                missing = set(self.tool_filter).difference(available)
                if missing:
                    raise ValueError("The selected MCP roster contains unavailable tool names.")
                result["tools"] = [available[name] for name in self.tool_filter]
            self._tool_names = {tool["name"] for tool in result["tools"]}
            result.pop("nextCursor", None)
            return result

    def _run_request(self, method: str, params: Mapping[str, Any], call: ToolCall | None = None) -> dict[str, Any]:
        """Preserve typed admission stops through the MCP SDK's task-group cleanup.

        Args:
            method: Selected remote MCP operation.
            params: Original call arguments with its already authorized tool name.
            call: Optional metered call whose ``sent`` flag marks the remote tool request.

        Returns:
            The actual protocol result from the selected endpoint.
        """
        # The token lookup runs before the event loop so a refresh or reauth
        # error keeps its own safe message instead of the generic one below.
        credentials = self._credentials()
        try:
            return asyncio.run(self._request(method, params, call, credentials))
        except BaseExceptionGroup as error:
            pending: list[BaseException] = [error]
            leaves = []
            while pending:
                current = pending.pop()
                if isinstance(current, BaseExceptionGroup):
                    pending.extend(current.exceptions)
                else:
                    leaves.append(current)
            if all(isinstance(item, BudgetReached | BudgetError | UsagePendingError) for item in leaves):
                raise leaves[0] from error
            raise ValueError("The selected MCP endpoint could not complete the tool request.") from error

    def dispatch(self, body: Mapping[str, Any]) -> ModelHTTPResult:
        """Serve a restricted MCP JSON-RPC request without accepting a guest-selected URL.

        Args:
            body: Original MCP SDK request through the scoped mailbox token.

        Returns:
            A standard MCP JSON-RPC response with the actual remote result.
        """
        method, identity = body.get("method"), body.get("id")
        params = body.get("params") or {}
        if body.get("jsonrpc") != "2.0" or not isinstance(params, Mapping):
            raise ValueError("The tool relay requires an MCP JSON-RPC request.")
        self.check_admission()
        if method == "notifications/initialized":
            return ModelHTTPResult(202, "application/json", b"")
        if method == "initialize":
            result = {
                "protocolVersion": types.LATEST_PROTOCOL_VERSION,
                "capabilities": {"tools": {}},
                "serverInfo": {"name": "app-scoped-tools", "version": "1"},
            }
        elif method == "tools/list":
            result = self._list()
        elif method == "tools/call":
            if self._tool_names is None:
                self._list()
            tool_name = str(params.get("name") or "")
            if tool_name not in (self._tool_names or set()):
                raise ValueError("The requested MCP tool is outside the selected roster.")
            if not isinstance(params.get("arguments") or {}, Mapping):
                raise ValueError("MCP tool arguments must be a JSON object.")
            arguments = dict(params.get("arguments") or {})
            call_id = uuid.uuid4().hex[:12]
            approved = self.authorize_tool is None or self.authorize_tool(call_id, tool_name, arguments)
            if approved:
                if self.meter is None:
                    result = self._run_request(method, params)
                else:
                    result = self.meter(
                        tool_name, arguments, ToolCall(lambda call: self._run_request(method, params, call))
                    )
            else:
                result = {"content": [{"type": "text", "text": "User declined"}], "isError": True}
        else:
            raise ValueError("The scoped tool relay does not permit this MCP method.")
        return ModelHTTPResult(
            200, "application/json", json.dumps({"jsonrpc": "2.0", "id": identity, "result": result}).encode()
        )
