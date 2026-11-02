"""Tests for the FastMCP mount's tool annotations and tool schemas."""

from __future__ import annotations

from types import SimpleNamespace
from typing import Annotated

from fastapi import Depends, FastAPI, Header
from fastapi.middleware.trustedhost import TrustedHostMiddleware
from fastmcp import Client, FastMCP
from fastmcp.server.providers.openapi.routing import MCPType, RouteMap
from pydantic import BaseModel

from core.api import mcp_mount
from core.api.mcp_mount import AGENT_TAG, _annotations_for_method, _annotations_for_route, _trim_tool_spec
from core.config import LOCAL_MODE_LOOPBACK_HOSTS


def test_annotations_for_method_reads_are_read_only() -> None:
    """Safe HTTP reads project a read-only hint."""
    for method in ("GET", "HEAD", "OPTIONS"):
        ann = _annotations_for_method(method)
        assert ann is not None
        assert ann.readOnlyHint is True


def test_annotations_for_method_delete_is_destructive() -> None:
    """DELETE projects a non-read-only, destructive hint."""
    ann = _annotations_for_method("DELETE")
    assert ann is not None
    assert ann.readOnlyHint is False
    assert ann.destructiveHint is True


def test_annotations_for_method_writes_are_mutating_not_destructive() -> None:
    """Body-bearing writes project a non-read-only, non-destructive hint."""
    for method in ("POST", "PUT", "PATCH"):
        ann = _annotations_for_method(method)
        assert ann is not None
        assert ann.readOnlyHint is False
        assert ann.destructiveHint is False


def test_annotations_for_method_unmapped_returns_none() -> None:
    """A verb with no clear approval semantics stays unannotated."""
    assert _annotations_for_method("TRACE") is None


def test_annotations_for_route_falls_back_to_method() -> None:
    """A route with no authored hints inherits the method default."""
    route = SimpleNamespace(method="GET", extensions={})
    ann = _annotations_for_route(route)
    assert ann is not None
    assert ann.readOnlyHint is True


def test_annotations_for_route_authored_hint_overrides_method() -> None:
    """An authored destructive hint merges over a POST's mutating default."""
    route = SimpleNamespace(
        method="POST", extensions={"x-mcp-annotations": {"destructiveHint": True}}
    )
    ann = _annotations_for_route(route)
    assert ann is not None
    assert ann.readOnlyHint is False
    assert ann.destructiveHint is True


def test_annotations_for_route_authored_hint_without_method_default() -> None:
    """Authored hints apply even when the verb itself maps to no default."""
    route = SimpleNamespace(
        method="TRACE", extensions={"x-mcp-annotations": {"destructiveHint": True}}
    )
    ann = _annotations_for_route(route)
    assert ann is not None
    assert ann.destructiveHint is True


class _GoalBody(BaseModel):
    """Request body for the fixture route."""

    goal: str = ""


def _auth_header_mcp(seen: list[str | None]) -> FastMCP:
    """Project one agent route that reads ``authorization`` as a header.

    Args:
        seen: Collects the ``Authorization`` value each call reached the route with.

    Returns:
        The FastMCP server built the same way ``mount_mcp_on_app`` builds it.
    """

    def user(authorization: Annotated[str | None, Header()] = None) -> str:
        seen.append(authorization)
        return "user"

    app = FastAPI()

    @app.post("/goal", operation_id="echo_goal", tags=[AGENT_TAG])
    def echo_goal(req: _GoalBody, current_user: str = Depends(user)) -> dict[str, str]:
        return {"goal": req.goal}

    return FastMCP.from_fastapi(
        app=app,
        route_maps=[RouteMap(tags={AGENT_TAG}, mcp_type=MCPType.TOOL), RouteMap(mcp_type=MCPType.EXCLUDE)],
        mcp_component_fn=_trim_tool_spec,
    )


async def test_tool_schema_hides_the_authorization_header() -> None:
    """The bearer header is never offered to the model as a tool argument."""
    async with Client(_auth_header_mcp([])) as client:
        tools = await client.list_tools()
    schema = next(t for t in tools if t.name == "echo_goal").inputSchema
    assert "authorization" not in schema.get("properties", {})
    assert "goal" in schema["properties"]


async def test_model_supplied_authorization_never_reaches_the_header() -> None:
    """A model-filled ``authorization`` argument cannot replace the bearer."""
    seen: list[str | None] = []
    async with Client(_auth_header_mcp(seen)) as client:
        await client.call_tool(
            "echo_goal",
            {"goal": "g", "authorization": "The user asked in this turn"},
            raise_on_error=False,
        )
    assert seen == [None]


async def test_tool_call_passes_the_local_mode_host_check(monkeypatch) -> None:
    """A tool call reaches a route behind the local-mode host allowlist.

    FastMCP calls the API in process with its own Host header. The Mac app
    refused FastMCP's default host with "400 Invalid host header".

    Args:
        monkeypatch: Records the FastMCP server that the mount builds.
    """
    built: list[FastMCP] = []
    real = FastMCP.from_fastapi

    def record(**kwargs: object) -> FastMCP:
        server = real(**kwargs)
        built.append(server)
        return server

    monkeypatch.setattr(mcp_mount.FastMCP, "from_fastapi", record)
    app = FastAPI()

    @app.get("/ping", operation_id="ping", tags=[AGENT_TAG])
    def ping() -> dict[str, str]:
        return {"ok": "yes"}

    app.add_middleware(TrustedHostMiddleware, allowed_hosts=sorted(LOCAL_MODE_LOOPBACK_HOSTS))
    mcp_mount.mount_mcp_on_app(app)
    async with Client(built[0]) as client:
        result = await client.call_tool("ping", {})
    assert result.data == {"ok": "yes"} or result.structured_content == {"ok": "yes"}
