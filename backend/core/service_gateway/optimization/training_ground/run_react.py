"""Resolve the MCP tool roster for a ReAct program.

:func:`resolve_react_tools` builds the roster from a live MCP listing or from a
persisted snapshot, and returns it with its schema-hash map. Each live tool
opens its own MCP session per call, so it works from any rollout thread.
"""

from __future__ import annotations

import asyncio
import json
import logging
from collections.abc import Mapping, Sequence
from typing import Any

import dspy
from dspy.adapters.types.tool import convert_input_schema_to_tool_args
from mcp import ClientSession
from mcp.client.streamable_http import streamablehttp_client

from core.billing.model_gateway import raise_gateway_stop
from core.config import Settings, app_env

from ..tool_overlay import hash_tool_schema

logger = logging.getLogger(__name__)

TERMINAL_TOOL_NAMES = frozenset({"submit", "finish"})
"""The synthetic loop-exit tool the program appends to every roster — ``submit``
on ReActV2, ``finish`` on classic ReAct. Excluded from the optimizable tool
surface (snapshot rosters, overlay) because it carries no user-tunable
description — it is DSPy's fixed loop-exit action."""

# A protected settings action (email codes, passkeys, linked sign-ins) must
# never run without a person who confirms it. Optimization rollouts call tools
# unattended, and a served react chat can rename tools or run in yolo, so
# neither roster lists these tools (SKY-266).
HUMAN_CONFIRM_ONLY_TOOLS: frozenset[str] = frozenset({"change_protected_setting"})


def _list_live_tools(mcp_url: str, auth_header: str | None) -> list[dspy.Tool]:
    """List the live MCP roster and wrap each tool for threaded sync rollouts.

    Each wrapper opens a fresh MCP session per invocation (see
    :func:`_live_mcp_tool`) so react rollouts can call tools synchronously
    from ``Evaluate``'s worker threads. A ``dspy.Tool.from_mcp_tool`` wrapper
    would be async and bound to a session that closes right after listing —
    every rollout-time call would fail.

    Args:
        mcp_url: MCP server URL.
        auth_header: Optional ``Authorization`` header to forward.

    Returns:
        List of dspy.Tool objects, one per MCP-exposed tool except
        :data:`HUMAN_CONFIRM_ONLY_TOOLS`.
    """
    relay_token = app_env("APP_TOOL_RELAY_TOKEN")
    if relay_token:
        mcp_url = f"{_relay_url().rstrip('/')}/_mcp"
        auth_header = f"Bearer {relay_token}"
    headers = {"Authorization": auth_header} if auth_header else None
    try:
        specs = asyncio.run(_list_live_tool_specs(mcp_url, headers))
    except Exception:
        _raise_tool_budget_stop()
        raise
    tools: list[dspy.Tool] = []
    for spec in specs:
        if spec.name in HUMAN_CONFIRM_ONLY_TOOLS:
            continue
        tools.append(_live_mcp_tool(mcp_url, headers, spec))
    return tools


def _raise_tool_budget_stop() -> None:
    """Restore authoritative budget stops before ReAct can treat them as tool observations."""
    token = app_env("APP_TOOL_RELAY_TOKEN")
    if token:
        raise_gateway_stop({"url": _relay_url(), "token": token})


def _relay_url() -> str:
    """Return the sandbox's budget relay URL.

    Returns:
        The relay URL from ``APP_BUDGET_RELAY_URL`` or its legacy name.

    Raises:
        KeyError: Neither name is set although a relay token is.
    """
    url = app_env("APP_BUDGET_RELAY_URL")
    if url is None:
        raise KeyError("APP_BUDGET_RELAY_URL")
    return url


async def _list_live_tool_specs(mcp_url: str, headers: dict[str, str] | None) -> list[Any]:
    """Fetch the raw MCP tool listing over one short-lived session.

    Args:
        mcp_url: MCP server URL.
        headers: Optional HTTP headers forwarded to the MCP transport.

    Returns:
        The server's ``mcp.types.Tool`` specs, in listing order.
    """
    async with (
        streamablehttp_client(mcp_url, headers=headers) as (read, write, _),
        ClientSession(read, write) as session,
    ):
        await session.initialize()
        listing = await session.list_tools()
        return list(listing.tools)


def _mcp_result_content(result: Any) -> Any:
    """Flatten an MCP ``CallToolResult`` into tool-observation content.

    Mirrors dspy's ``_convert_mcp_tool_result`` so the wrapped tools return
    the same shapes ``dspy.Tool.from_mcp_tool`` wrappers would.

    Args:
        result: The ``mcp.types.CallToolResult`` from ``session.call_tool``.

    Returns:
        A single string for one text block, a list of strings for several, or
        the raw non-text content blocks when no text was returned.

    Raises:
        RuntimeError: When the server flagged the call as an error.
    """
    texts = [c.text for c in result.content if getattr(c, "text", None) is not None]
    content: Any = texts[0] if len(texts) == 1 else texts
    if result.isError:
        raise RuntimeError(f"Failed to call a MCP tool: {content}")
    return content or [c for c in result.content if getattr(c, "text", None) is None]


def _live_mcp_tool(mcp_url: str, headers: dict[str, str] | None, spec: Any) -> dspy.Tool:
    """Build a synchronously callable ``dspy.Tool`` for one live MCP tool.

    Args:
        mcp_url: MCP server URL.
        headers: Optional HTTP headers forwarded to the MCP transport.
        spec: The ``mcp.types.Tool`` spec to wrap.

    Returns:
        A ``dspy.Tool`` whose body opens a fresh MCP session per call, so it
        stays valid for the whole run and is callable from any rollout thread.
    """
    args, arg_types, arg_desc = convert_input_schema_to_tool_args(spec.inputSchema)

    async def _call_async(**kwargs: Any) -> Any:
        """Run one tool call over its own session on the caller's loop."""
        async with (
            streamablehttp_client(mcp_url, headers=headers) as (read, write, _),
            ClientSession(read, write) as session,
        ):
            await session.initialize()
            result = await session.call_tool(spec.name, arguments=kwargs)
            return _mcp_result_content(result)

    def _call(**kwargs: Any) -> Any:
        """Invoke the MCP tool over a fresh session on a private event loop."""
        try:
            return asyncio.run(_call_async(**kwargs))
        except Exception:
            _raise_tool_budget_stop()
            raise

    _call.__name__ = spec.name
    _call.__doc__ = spec.description
    return dspy.Tool(
        _call,
        name=spec.name,
        desc=spec.description,
        args=args,
        arg_types=arg_types,
        arg_desc=arg_desc,
    )


def _as_sequence(value: Any) -> list[Any]:
    """Coerce a cell value into a list, tolerating JSON-string cells.

    Args:
        value: Raw cell value (sequence, JSON string, or other).

    Returns:
        A list — empty when the value is missing or not array-shaped.
    """
    if isinstance(value, list | tuple):
        return list(value)
    if isinstance(value, str) and value.strip():
        try:
            parsed = json.loads(value)
        except json.JSONDecodeError:
            return []
        return list(parsed) if isinstance(parsed, list | tuple) else []
    return []


def _dataset_snapshot_tools(
    dataset: Sequence[Mapping[str, Any]] | None,
) -> list[dspy.Tool]:
    """Rebuild a ``dspy.Tool`` roster from a dataset-carried snapshot sidecar.

    A persisted snapshot tool source carries the roster on a reserved
    ``__tool_snapshot__`` key (the first row that has it wins), a list of specs::

        {"name": str, "description": str, "args": {<arg>: {<schema>}}}

    Each spec becomes a ``dspy.Tool`` whose ``name``/``desc``/``args``
    are read for the seed instructions, schema-hash snapshot, and overlay. The
    serve path uses this to reconstruct the surface a run was optimized against.

    Args:
        dataset: Raw dataset rows; scanned for the snapshot sidecar.

    Returns:
        One ``dspy.Tool`` per snapshot spec (``submit`` excluded), in spec
        order. Empty when no sidecar is present.
    """
    specs = _find_snapshot_specs(dataset)
    tools: list[dspy.Tool] = []
    for spec in specs:
        if not isinstance(spec, Mapping):
            continue
        name = str(spec.get("name") or "").strip()
        if not name or name in TERMINAL_TOOL_NAMES:
            continue
        desc = str(spec.get("description") or spec.get("desc") or "")
        args = spec.get("args") if isinstance(spec.get("args"), Mapping) else {}
        tools.append(_snapshot_tool(name=name, desc=desc, args=dict(args)))
    return tools


def _find_snapshot_specs(
    dataset: Sequence[Mapping[str, Any]] | None,
) -> list[Any]:
    """Return the tool-snapshot spec list from the dataset sidecar.

    Args:
        dataset: Raw dataset rows.

    Returns:
        The first row's ``__tool_snapshot__`` list, or an empty list when no
        row carries one.
    """
    if not dataset:
        return []
    for row in dataset:
        if not isinstance(row, Mapping):
            continue
        sidecar = row.get("__tool_snapshot__")
        if isinstance(sidecar, str):
            sidecar = _as_sequence(sidecar)
        if isinstance(sidecar, list | tuple) and sidecar:
            return list(sidecar)
    return []


def _snapshot_tool(*, name: str, desc: str, args: dict[str, Any]) -> dspy.Tool:
    """Construct a ``dspy.Tool`` carrying a snapshot spec's schema.

    The serve path reconstructs the roster from these specs; the callable body
    is a placeholder because serve re-binds live MCP callables when it executes.

    Args:
        name: Tool name.
        desc: Tool description.
        args: Per-arg JSON schema map.

    Returns:
        A ``dspy.Tool`` whose ``args`` mirror the snapshot spec.
    """

    def _noop(**_kwargs: Any) -> None:
        """Placeholder body — serve re-binds live callables before executing."""

    _noop.__name__ = name
    _noop.__doc__ = desc
    return dspy.Tool(_noop, name=name, desc=desc, args=args or None)


def resolve_react_tools(
    tool_source: Any,
    signature_cls: type,
    settings: Settings,
    *,
    dataset: Sequence[Mapping[str, Any]] | None = None,
) -> tuple[list[dspy.Tool], dict[str, str]]:
    """Materialise the react tool roster + its schema-hash snapshot.

    Two sources, mirroring ``ToolSource.kind``:

    - ``live_mcp`` lists the live MCP roster via :func:`_list_live_tools`,
      optionally filtering and reordering by ``tool_source.tool_filter``.
    - ``dataset_snapshot`` rebuilds the roster from the dataset's reserved
      ``__tool_snapshot__`` sidecar (run path) or the source's own persisted
      ``tool_snapshot`` specs (serve path) via :func:`_dataset_snapshot_tools`.

    The schema-hash map is computed with :func:`hash_tool_schema` over whatever
    roster was resolved, so it stays 1:1 with the tools the seed program sees.

    Args:
        tool_source: A ``ToolSource``-shaped object or a persisted mapping
            (``kind`` plus optional ``mcp_url``/``mcp_auth_header``/
            ``tool_filter``/``tool_snapshot``); read field-tolerantly via
            :func:`_ts` so the run and serve paths share one resolver.
        signature_cls: The resolved signature (unused today; accepted so the
            call site stays stable as the snapshot path grows).
        settings: Runtime settings — supplies the default MCP URL.
        dataset: Raw dataset rows, required for the ``dataset_snapshot`` kind.

    Returns:
        ``(tools, schema_hashes)`` — the resolved roster and its
        ``{tool_name: sha256_hex}`` snapshot.

    Raises:
        ValueError: When ``tool_source.kind`` is unrecognised, or when a
            snapshot source carries no tools.
    """
    _ = signature_cls
    kind = _ts(tool_source, "kind")
    if kind == "live_mcp":
        mcp_url = _ts(tool_source, "mcp_url") or settings.generalist_agent_mcp_url
        tools = _list_live_tools(mcp_url, _ts(tool_source, "mcp_auth_header"))
        tools = _apply_tool_filter(tools, _ts(tool_source, "tool_filter"))
    elif kind == "dataset_snapshot":
        tools = _dataset_snapshot_tools(_snapshot_source(tool_source, dataset))
        tools = _apply_tool_filter(tools, _ts(tool_source, "tool_filter"))
        if not tools:
            raise ValueError(
                "dataset_snapshot tool source carried no tools — expected a "
                "'__tool_snapshot__' sidecar with at least one spec."
            )
    else:
        raise ValueError(f"Unknown tool_source.kind {kind!r}.")
    schema_hashes = {tool.name: hash_tool_schema(tool) for tool in tools}
    return tools, schema_hashes


def _ts(tool_source: Any, key: str, default: Any = None) -> Any:
    """Read ``key`` off a tool source that may be an object or a mapping.

    The run path passes a ``ToolSource`` model (attribute access); the serve
    path re-sources from a persisted dict (mapping access). Reads through
    both so the same resolver serves either shape.

    Args:
        tool_source: A ``ToolSource``-shaped object or a plain mapping.
        key: Field name to read.
        default: Returned when the key is absent on either shape.

    Returns:
        The stored value, or ``default`` when absent.
    """
    if isinstance(tool_source, Mapping):
        return tool_source.get(key, default)
    return getattr(tool_source, key, default)


def _snapshot_source(
    tool_source: Any, dataset: Sequence[Mapping[str, Any]] | None
) -> Sequence[Mapping[str, Any]] | None:
    """Pick the snapshot-spec carrier for a ``dataset_snapshot`` source.

    Serve persists the snapshot specs on the source itself under
    ``tool_snapshot``; the run path carries them on the dataset's
    ``__tool_snapshot__`` sidecar. When the source carries them, wrap the
    specs in a synthetic single-row sidecar so :func:`_dataset_snapshot_tools`
    can read them without the original dataset.

    Args:
        tool_source: The ``dataset_snapshot`` tool source (object or mapping).
        dataset: Raw dataset rows for the run path.

    Returns:
        A row sequence carrying the snapshot sidecar, or ``dataset`` when the
        source has no persisted ``tool_snapshot``.
    """
    snapshot = _ts(tool_source, "tool_snapshot")
    if snapshot:
        return [{"__tool_snapshot__": snapshot}]
    return dataset


def _apply_tool_filter(tools: list[dspy.Tool], tool_filter: list[str] | None) -> list[dspy.Tool]:
    """Filter and order a tool roster by ``tool_filter``.

    When ``tool_filter`` is set the result keeps only the named tools, in the
    filter's order (names absent from the roster are skipped). When ``None`` the
    roster is returned unchanged.

    Args:
        tools: The resolved tool roster.
        tool_filter: Optional ordered allow-list of tool names.

    Returns:
        The filtered, reordered roster (or the original when no filter).
    """
    if not tool_filter:
        return tools
    by_name = {tool.name: tool for tool in tools}
    return [by_name[name] for name in tool_filter if name in by_name]


__all__ = ["resolve_react_tools"]
