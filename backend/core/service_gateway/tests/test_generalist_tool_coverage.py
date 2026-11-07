"""Tests that keep the generalist agent's tool lists in step with the app's agent routes."""

from __future__ import annotations

from unittest.mock import patch

import pytest

from core.api.app import create_app
from core.registry.core import ServiceRegistry
from core.service_gateway.agents.generalist import (
    _ALWAYS_TOOLS,
    _DATASET_READY_TOOLS,
    _DESTRUCTIVE_TOOLS,
    _POST_SUBMIT_TOOLS,
    _SAFE_MUTATIONS,
    _needs_approval,
)

# Agent-tagged routes the generalist deliberately does not see. Each one has a
# reason; a new agent route must be added to a generalist list or here.
_HIDDEN_FROM_GENERALIST = {
    # Every run optimizes a repository, and the wizard's own Run button submits it (SKY-316).
    "submit_unified_run_optimize_post",
    # Grid-search pairs are DSPy programs, and these routes refuse every call with 410 (SKY-316).
    "serve_pair_info_serve",
    "request_user_pair_inference",
    # The chat's inference card runs this itself; the agent cannot know the user's inputs.
    "serve_program_serve",
    # Needs a black-box run ordinal the agent has no listing for, and returns a full transcript.
    "get_agent_run_optimizations",
    # Folder organizing and the per-run cost breakdown are their own feature;
    # this list keeps them out until that change gives them to the generalist.
    "list_folders",
    "list_folder_runs",
    "list_runs_for_organizing",
    "create_folder",
    "update_folder",
    "move_folder",
    "move_runs_to_folder",
    "delete_folder",
    "get_usage_optimizations",
    # Metric hardening is a new feature; the generalist gets hardening tools in a later change.
    "create_hardening_job",
    "get_hardening_job",
    "get_latest_hardening_job",
    "set_hardening_exploit_verdict",
    "decide_hardening_fix",
    "retry_hardening_job",
}

_FOLDER_WRITES = ("create_folder", "update_folder", "move_folder", "move_runs_to_folder", "delete_folder")


@pytest.fixture(scope="module")
def agent_tool_names() -> set[str]:
    """Return the MCP tool names the full app projects from its agent-tagged routes.

    FastMCP names each tool by the operation id cut at its first ``__``.

    Returns:
        The set of tool names the generalist's MCP session can list.
    """
    with (
        patch("core.api.app.get_job_store"),
        patch("core.api.app.get_worker"),
        patch("core.api.app.DspyService"),
    ):
        app = create_app(registry=ServiceRegistry())
    names: set[str] = set()
    for operations in app.openapi()["paths"].values():
        for operation in operations.values():
            if "agent" in (operation.get("tags") or []):
                names.add(operation["operationId"].split("__")[0])
    return names


def test_every_generalist_tool_name_matches_an_agent_route(agent_tool_names: set[str]) -> None:
    """A tool name with no agent route would silently drop out of the agent's tool list."""
    listed = _ALWAYS_TOOLS | _DATASET_READY_TOOLS | _POST_SUBMIT_TOOLS
    assert sorted(listed - agent_tool_names) == []


def test_every_gated_tool_name_matches_an_agent_route(agent_tool_names: set[str]) -> None:
    """A stale approval entry means a renamed tool lost its approval card."""
    assert sorted((_DESTRUCTIVE_TOOLS | _SAFE_MUTATIONS) - agent_tool_names) == []


def test_every_agent_route_is_exposed_or_deliberately_hidden(agent_tool_names: set[str]) -> None:
    """A new agent route must be given to the generalist or listed with a reason it is hidden."""
    listed = _ALWAYS_TOOLS | _DATASET_READY_TOOLS | _POST_SUBMIT_TOOLS
    assert sorted(agent_tool_names - listed - _HIDDEN_FROM_GENERALIST) == []
    assert sorted(_HIDDEN_FROM_GENERALIST - agent_tool_names) == []


def test_folder_writes_keep_their_approval_cards() -> None:
    """Exposed folder writes still ask first; the ones that change access ask even in auto-safe."""
    for name in _FOLDER_WRITES:
        assert _needs_approval(name, "ask") is True
    for name in ("update_folder", "move_folder", "move_runs_to_folder", "delete_folder"):
        assert _needs_approval(name, "auto_safe") is True
    assert _needs_approval("create_folder", "auto_safe") is False
