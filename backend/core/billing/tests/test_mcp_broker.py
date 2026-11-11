"""Exercise the actual MCP SDK over a fixed, credential-private mocked HTTP transport."""

from __future__ import annotations

import json
import socket
from collections.abc import Mapping
from typing import Any

import httpx
import pytest
from mcp import types

from core.billing import mcp_broker
from core.billing.signals import BudgetReached
from core.service_gateway.optimization.training_ground import run_react
from core.worker.scoped_relay import model_forwarder


def _rpc(method: str, params: dict[str, Any] | None = None) -> dict[str, Any]:
    """Build one standard client request without embedding endpoint information."""
    return {"jsonrpc": "2.0", "id": 7, "method": method, "params": params or {}}


@pytest.fixture
def remote(monkeypatch: pytest.MonkeyPatch) -> list[httpx.Request]:
    """Provide a real MCP handshake and tools protocol without opening a network socket."""
    requests = []

    def addresses(*_args: Any, **_kwargs: Any) -> list[tuple]:
        """Resolve a public test origin once; the mock transport never dials this address."""
        return [(socket.AF_INET, socket.SOCK_STREAM, 0, "", ("8.8.8.8", 443))]

    def respond(request: httpx.Request) -> httpx.Response:
        """Return actual MCP response shapes while recording the transport boundary."""
        requests.append(request)
        if request.method != "POST":
            return httpx.Response(405)
        body = json.loads(request.content)
        method = body["method"]
        if method == "notifications/initialized":
            return httpx.Response(202)
        if method == "initialize":
            result = {
                "protocolVersion": types.LATEST_PROTOCOL_VERSION,
                "capabilities": {"tools": {}},
                "serverInfo": {"name": "actual-test-endpoint", "version": "1"},
            }
        elif method == "tools/list":
            result = {
                "tools": [
                    {"name": "echo", "description": "Echo the argument.", "inputSchema": {"type": "object"}},
                    {"name": "other", "description": "Unselected tool.", "inputSchema": {"type": "object"}},
                ]
            }
        elif method == "tools/call":
            value = body["params"]["arguments"]["value"]
            text = request.headers["authorization"] if value == "echo-auth" else value
            result = {"content": [{"type": "text", "text": text}], "isError": False}
        else:
            pytest.fail(f"Unexpected remote method {method}")
        return httpx.Response(200, json={"jsonrpc": "2.0", "id": body["id"], "result": result})

    monkeypatch.setattr(mcp_broker.socket, "getaddrinfo", addresses)
    monkeypatch.setattr(mcp_broker.httpx, "AsyncHTTPTransport", lambda **_kwargs: httpx.MockTransport(respond))
    return requests


def test_sdk_tool_result_uses_pinned_parent_auth_and_selected_roster(remote: list[httpx.Request]) -> None:
    """Preserve the remote tool result while withholding URL and provider credentials from the guest."""
    broker = mcp_broker.McpToolsBroker(
        "https://tools.example/mcp?private=query",
        check_admission=lambda: None,
        auth_header="Bearer parent-only-secret",
        tool_filter=["echo"],
    )
    listing = json.loads(broker.dispatch(_rpc("tools/list")).body)
    assert [tool["name"] for tool in listing["result"]["tools"]] == ["echo"]
    result = json.loads(
        broker.dispatch(_rpc("tools/call", {"name": "echo", "arguments": {"value": "actual answer"}})).body
    )
    assert result["result"]["content"] == [{"type": "text", "text": "actual answer"}]
    assert "parent-only-secret" not in json.dumps(result)
    assert all(request.url.host == "8.8.8.8" for request in remote)
    assert all(request.headers["host"] == "tools.example" for request in remote)
    assert all(request.extensions["sni_hostname"] == "tools.example" for request in remote)
    assert all(request.headers["authorization"] == "Bearer parent-only-secret" for request in remote)
    calls = [json.loads(request.content) for request in remote if request.method == "POST"]
    assert sum(body["method"] == "tools/call" for body in calls) == 1
    count = len(remote)
    with pytest.raises(ValueError, match="outside the selected roster"):
        broker.dispatch(_rpc("tools/call", {"name": "other"}))
    with pytest.raises(ValueError, match="does not permit"):
        broker.dispatch(_rpc("resources/read", {"uri": "http://internal"}))
    assert len(remote) == count


def test_mcp_response_cannot_echo_parent_authorization_to_guest(remote: list[httpx.Request]) -> None:
    """Redact both the full authorization header and its token from tool output."""
    broker = mcp_broker.McpToolsBroker(
        "https://tools.example/mcp?access_token=query-parent-secret",
        check_admission=lambda: None,
        auth_header="Bearer parent-only-secret",
        tool_filter=["echo"],
    )
    broker.dispatch(_rpc("tools/list"))

    response = broker.dispatch(_rpc("tools/call", {"name": "echo", "arguments": {"value": "echo-auth"}}))

    assert "parent-only-secret" not in response.body.decode()
    assert "query-parent-secret" not in response.body.decode()
    assert "[REDACTED]" in response.body.decode()


def test_declined_tool_call_never_reaches_remote_endpoint(remote: list[httpx.Request]) -> None:
    """Return a tool error without dispatch after caller denial."""
    approvals: list[tuple[str, str, dict[str, Any]]] = []

    def deny(call_id: str, tool_name: str, arguments: dict[str, Any]) -> bool:
        """Record the displayed request and deny its physical side effect."""
        approvals.append((call_id, tool_name, arguments))
        return False

    broker = mcp_broker.McpToolsBroker(
        "https://tools.example/mcp",
        check_admission=lambda: None,
        tool_filter=["echo"],
        authorize_tool=deny,
    )
    result = json.loads(
        broker.dispatch(_rpc("tools/call", {"name": "echo", "arguments": {"value": "blocked"}})).body
    )

    calls = [json.loads(request.content) for request in remote if request.method == "POST"]
    assert sum(body["method"] == "tools/call" for body in calls) == 0
    assert result["result"]["isError"] is True
    assert result["result"]["content"][0]["text"] == "User declined"
    assert approvals[0][1:] == ("echo", {"value": "blocked"})


@pytest.mark.parametrize("address", ["127.0.0.1", "10.0.0.1", "169.254.169.254", "::1", "fd00:ec2::254"])
def test_private_destinations_are_denied_by_default(monkeypatch: pytest.MonkeyPatch, address: str) -> None:
    """Reject private destinations before creating any MCP session."""
    monkeypatch.setattr(
        mcp_broker.socket,
        "getaddrinfo",
        lambda *_args, **_kwargs: [(socket.AF_INET, socket.SOCK_STREAM, 0, "", (address, 80))],
    )
    with pytest.raises(ValueError, match=r"private MCP|metadata-service"):
        mcp_broker.McpToolsBroker("https://tools.example/mcp", check_admission=lambda: None)


def test_budget_stop_after_initialize_remains_base_exception(remote: list[httpx.Request]) -> None:
    """Unwrap the SDK task group without converting a normal budget stop into tool failure."""
    checks = []

    def check() -> None:
        """Trip admission after initialization and before the physical tools/list request."""
        checks.append(True)
        if len(checks) == 3:
            raise BudgetReached("budget gate")

    broker = mcp_broker.McpToolsBroker("https://tools.example/mcp", check_admission=check)
    with pytest.raises(BudgetReached, match="budget gate"):
        broker.dispatch(_rpc("tools/list"))
    assert all(json.loads(request.content)["method"] != "tools/list" for request in remote if request.method == "POST")


def test_react_listing_uses_scoped_guest_relay(monkeypatch: pytest.MonkeyPatch) -> None:
    """Use the scoped local transport while keeping the original tool-source payload separate."""
    requests = []

    async def listing(url: str, headers: dict[str, str]) -> list[Any]:
        """Capture the live resolver's actual endpoint without a network call."""
        requests.append((url, headers))
        return []

    monkeypatch.setenv("SKYNET_BUDGET_RELAY_URL", "http://127.0.0.1:9000/v1")
    monkeypatch.setenv("SKYNET_TOOL_RELAY_TOKEN", "scoped-tool")
    monkeypatch.setattr(run_react, "_list_live_tool_specs", listing)
    assert run_react._list_live_tools("https://original/mcp", None) == []
    assert requests == [("http://127.0.0.1:9000/v1/_mcp", {"Authorization": "Bearer scoped-tool"})]


def test_worker_tool_token_cannot_dispatch_models() -> None:
    """Keep tool and model capabilities disjoint even though they share one mailbox."""
    dispatch = model_forwarder({"_skynet_tools_route": {"url": "http://127.0.0.1:9000/v1", "token": "tool"}})
    with pytest.raises(ValueError, match="unknown metered model capability"):
        dispatch("tool", "/v1/chat/completions", {}, {})


def test_parent_headers_reach_remote_and_are_redacted_from_tool_output(remote: list[httpx.Request]) -> None:
    """Send a connector's key header to the remote and keep it out of the guest's result."""

    def respond_with_header(tool: str, arguments: Mapping[str, Any], call: Any) -> dict[str, Any]:
        """Run the call unchanged; the meter only wraps it."""
        return call()

    broker = mcp_broker.McpToolsBroker(
        "https://tools.example/mcp",
        check_admission=lambda: None,
        headers={"X-Goog-Api-Key": "maps-parent-secret"},
        tool_filter=["echo"],
        meter=respond_with_header,
    )
    result = broker.dispatch(_rpc("tools/call", {"name": "echo", "arguments": {"value": "maps-parent-secret"}}))

    assert all(request.headers["x-goog-api-key"] == "maps-parent-secret" for request in remote)
    assert "maps-parent-secret" not in result.body.decode()
    assert "[REDACTED]" in result.body.decode()


def test_meter_wraps_each_tool_call_but_not_listing(remote: list[httpx.Request]) -> None:
    """Meter tools/call once per call and leave tools/list free."""
    metered: list[tuple[str, dict[str, Any]]] = []

    def meter(tool: str, arguments: Mapping[str, Any], call: Any) -> dict[str, Any]:
        """Record the metered call, then run it."""
        metered.append((tool, dict(arguments)))
        return call()

    broker = mcp_broker.McpToolsBroker(
        "https://tools.example/mcp", check_admission=lambda: None, tool_filter=["echo"], meter=meter
    )
    broker.dispatch(_rpc("tools/list"))
    result = json.loads(broker.dispatch(_rpc("tools/call", {"name": "echo", "arguments": {"value": "paid"}})).body)

    assert metered == [("echo", {"value": "paid"})]
    assert result["result"]["content"] == [{"type": "text", "text": "paid"}]
    with pytest.raises(ValueError, match="outside the selected roster"):
        broker.dispatch(_rpc("tools/call", {"name": "other", "arguments": {}}))
    assert len(metered) == 1


def test_meter_refusal_keeps_call_away_from_remote(remote: list[httpx.Request]) -> None:
    """Refuse a call the budget cannot cover before the remote sees tools/call."""

    def refuse(tool: str, arguments: Mapping[str, Any], call: Any) -> dict[str, Any]:
        """Refuse like a budget that has no room left."""
        raise BudgetReached("budget")

    broker = mcp_broker.McpToolsBroker(
        "https://tools.example/mcp", check_admission=lambda: None, tool_filter=["echo"], meter=refuse
    )
    with pytest.raises(BudgetReached):
        broker.dispatch(_rpc("tools/call", {"name": "echo", "arguments": {"value": "x"}}))
    calls = [json.loads(request.content) for request in remote if request.method == "POST"]
    assert all(body["method"] != "tools/call" for body in calls)


def test_connector_tokens_reach_only_tool_paths() -> None:
    """Register each connector route for tools and budget state, never for models."""
    dispatch = model_forwarder(
        {
            "_skynet_connector_routes": {
                "maps": {"url": "http://127.0.0.1:9000/v1", "token": "maps-token"},
                "browser": {"url": "http://127.0.0.1:9000/v1", "token": "browser-token"},
            }
        }
    )
    for token in ("maps-token", "browser-token"):
        with pytest.raises(ValueError, match="unknown metered model capability"):
            dispatch(token, "/v1/chat/completions", {}, {})
    with pytest.raises(ValueError, match="loopback gateway"):
        model_forwarder(
            {"_skynet_connector_routes": {"maps": {"url": "https://mapstools.googleapis.com/v1", "token": "t"}}}
        )
