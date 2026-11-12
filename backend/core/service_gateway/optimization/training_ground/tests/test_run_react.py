"""Unit tests for the react tool roster helpers.

Covers the ``dataset_snapshot`` branch of ``resolve_react_tools`` (roster
rebuild from the reserved sidecar / persisted specs).
"""

from __future__ import annotations

from types import SimpleNamespace

import pytest

from core.config import settings
from core.models.submissions import ToolSource
from core.service_gateway.optimization.training_ground.registry import hash_tool_schema
from core.service_gateway.optimization.training_ground.run_react import (
    resolve_react_tools,
)


def test_resolve_react_tools_dataset_snapshot_rebuilds_roster() -> None:
    """A dataset snapshot sidecar rebuilds a dspy.Tool roster + schema hashes."""
    snapshot = [
        {
            "name": "search",
            "description": "Search the web.",
            "args": {"q": {"type": "string", "description": "query"}},
        },
        {"name": "lookup", "description": "Look up an id.", "args": {}},
        {"name": "submit", "description": "reserved", "args": {}},
    ]
    dataset = [
        {"question": "x", "__tool_snapshot__": snapshot},
        {"question": "y"},
    ]
    tool_source = ToolSource(kind="dataset_snapshot")

    tools, hashes = resolve_react_tools(tool_source, signature_cls=object, settings=settings, dataset=dataset)

    names = [tool.name for tool in tools]
    assert names == ["search", "lookup"]
    assert "submit" not in names
    search_tool = next(tool for tool in tools if tool.name == "search")
    assert search_tool.desc == "Search the web."
    assert set(hashes) == {"search", "lookup"}
    assert hashes["search"] == hash_tool_schema(search_tool)


def test_resolve_react_tools_dataset_snapshot_honours_tool_filter() -> None:
    """``tool_filter`` keeps and reorders the snapshot roster."""
    snapshot = [
        {"name": "alpha", "description": "a", "args": {}},
        {"name": "beta", "description": "b", "args": {}},
        {"name": "gamma", "description": "c", "args": {}},
    ]
    dataset = [{"__tool_snapshot__": snapshot}]
    tool_source = ToolSource(kind="dataset_snapshot", tool_filter=["gamma", "alpha"])

    tools, _ = resolve_react_tools(tool_source, signature_cls=object, settings=settings, dataset=dataset)

    assert [tool.name for tool in tools] == ["gamma", "alpha"]


def test_resolve_react_tools_dataset_snapshot_without_sidecar_raises() -> None:
    """A dataset_snapshot source with no sidecar is a hard error."""
    tool_source = ToolSource(kind="dataset_snapshot")
    with pytest.raises(ValueError, match="carried no tools"):
        resolve_react_tools(tool_source, signature_cls=object, settings=settings, dataset=[{"q": "x"}])


def test_resolve_react_tools_unknown_kind_raises() -> None:
    """An unrecognised tool_source kind fails fast."""
    bad_source = SimpleNamespace(kind="carrier_pigeon", tool_filter=None)
    with pytest.raises(ValueError, match=r"Unknown tool_source\.kind"):
        resolve_react_tools(bad_source, signature_cls=object, settings=settings)


def test_resolve_react_tools_accepts_persisted_dict_snapshot() -> None:
    """A persisted dict tool_source (serve path) rebuilds the roster without a dataset."""
    persisted = {
        "kind": "dataset_snapshot",
        "tool_filter": ["search"],
        "tool_snapshot": [
            {
                "name": "search",
                "description": "Search the web.",
                "args": {"q": {"type": "string"}},
            },
            {"name": "lookup", "description": "Look up an id.", "args": {}},
        ],
    }

    tools, hashes = resolve_react_tools(persisted, signature_cls=object, settings=settings, dataset=None)

    assert [tool.name for tool in tools] == ["search"]
    assert set(hashes) == {"search"}
    assert hashes["search"] == hash_tool_schema(tools[0])
