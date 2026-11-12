"""Tests for the read-only GitHub MCP tools of the setup agent."""

import asyncio
import json
import shutil
import stat
import sys
from collections.abc import AsyncGenerator
from contextlib import asynccontextmanager
from pathlib import Path

import dspy
import pytest
from mcp.types import Tool, ToolAnnotations

from ..agents import generalist
from ..agents.github_mcp import (
    MAX_RESULT_CHARS,
    READ_TOOLS,
    GitHubMcpError,
    github_repo_tools,
    read_only_tools,
    server_parameters,
)

TOKEN = "test-token-not-real"

# A stand-in github-mcp-server: it records its argv and whether the token
# arrived in the environment, and serves two read tools, one write tool and
# one read tool that is not on the allow list.
_FAKE_SERVER = """
import json, os, sys
from mcp.server.fastmcp import FastMCP
from mcp.types import EmbeddedResource, TextContent, TextResourceContents, ToolAnnotations

with open(sys.argv[1], "w") as fh:
    json.dump({"argv": sys.argv[2:], "token": os.environ.get("GITHUB_PERSONAL_ACCESS_TOKEN")}, fh)
server = FastMCP("fake-github")
read = ToolAnnotations(readOnlyHint=True)

@server.tool(annotations=read)
def search_repositories(query: str) -> str:
    return "found " + query

@server.tool(annotations=read)
def get_file_contents(owner: str, repo: str, path: str) -> list:
    # The real server answers a file read with a status line and the body as
    # an embedded resource; a directory read with several text parts.
    if path.startswith("parts:"):
        return [TextContent(type="text", text="p" * int(path[6:])) for _ in range(3)]
    body = "x" * int(path) if path.isdigit() else "contents"
    resource = TextResourceContents(uri=f"repo://{owner}/{repo}/contents/{path}", mimeType="text/plain", text=body)
    return [
        TextContent(type="text", text="successfully downloaded text file (SHA: abc123)"),
        EmbeddedResource(type="resource", resource=resource),
    ]

@server.tool(annotations=ToolAnnotations(readOnlyHint=False))
def create_or_update_file(owner: str, repo: str, path: str, content: str) -> str:
    return "written"

@server.tool(annotations=read)
def list_repository_collaborators(owner: str, repo: str) -> str:
    return "people"

server.run()
"""


def _fake_binary(tmp_path: Path) -> tuple[Path, Path]:
    """Write an executable stand-in for ``github-mcp-server``.

    Args:
        tmp_path: Directory for the script and its record file.

    Returns:
        The executable and the file it records its launch in.
    """
    script = tmp_path / "fake_server.py"
    script.write_text(_FAKE_SERVER)
    record = tmp_path / "launch.json"
    binary = tmp_path / "github-mcp-server"
    binary.write_text(f'#!/bin/sh\nexec "{sys.executable}" "{script}" "{record}" "$@"\n')
    binary.chmod(binary.stat().st_mode | stat.S_IXUSR)
    return binary, record


def _names(tools: list[dspy.Tool]) -> list[str]:
    """Return the sorted names of the tools.

    Args:
        tools: The tools of one launch.

    Returns:
        The names, each checked to be present.
    """
    names = [tool.name for tool in tools]
    assert all(name is not None for name in names)
    return sorted(name for name in names if name is not None)


def _tool(name: str, read_only: bool | None) -> Tool:
    """Build a tool listing entry.

    Args:
        name: Tool name.
        read_only: The ``readOnlyHint`` annotation, or ``None`` for no annotations.

    Returns:
        The listing entry.
    """
    annotations = None if read_only is None else ToolAnnotations(readOnlyHint=read_only)
    return Tool(name=name, inputSchema={"type": "object"}, annotations=annotations)


def test_launch_is_read_only_repos_and_keeps_the_token_out_of_argv() -> None:
    """The server runs read-only with only the repos toolset, and gets the token by environment."""
    params = server_parameters("/usr/local/bin/github-mcp-server", TOKEN)
    assert params.args == ["stdio", "--read-only", "--toolsets", "repos"]
    assert TOKEN not in " ".join([params.command, *params.args])
    assert params.env == {"GITHUB_PERSONAL_ACCESS_TOKEN": TOKEN}


def test_only_allow_listed_read_tools_pass() -> None:
    """Write tools, unlisted tools and tools without a read-only mark are dropped."""
    tools = [
        _tool("search_repositories", True),
        _tool("get_file_contents", True),
        _tool("create_or_update_file", False),
        _tool("list_branches", None),
        _tool("list_repository_collaborators", True),
    ]
    assert [tool.name for tool in read_only_tools(tools)] == ["search_repositories", "get_file_contents"]


def test_missing_token_gives_a_clear_error() -> None:
    """Without a linked GitHub token, the tools do not start and the error says to connect GitHub."""

    async def run() -> None:
        """Open the tools with no token."""
        async with github_repo_tools("github-mcp-server", None):
            pass

    with pytest.raises(GitHubMcpError, match="press Connect GitHub"):
        asyncio.run(run())


def test_missing_binary_gives_a_clear_error() -> None:
    """A missing executable names the pinned version to install."""

    async def run() -> None:
        """Open the tools with an executable that does not exist."""
        async with github_repo_tools("no-such-github-mcp-server", TOKEN):
            pass

    with pytest.raises(GitHubMcpError, match=r"install github-mcp-server 2\.1\.0"):
        asyncio.run(run())


def test_tools_are_offered_read_only_and_callable(tmp_path: Path) -> None:
    """A server launch yields only the read tools, and a call reaches the server."""
    binary, record = _fake_binary(tmp_path)

    async def run() -> tuple[list[str], str]:
        """List the tools and call the repository search.

        Returns:
            The tool names and the search result.
        """
        async with github_repo_tools(str(binary), TOKEN) as tools:
            search = next(tool for tool in tools if tool.name == "search_repositories")
            return _names(tools), str(await search.acall(query="personal website"))

    names, found = asyncio.run(run())
    assert names == ["get_file_contents", "search_repositories"]
    assert "found personal website" in found
    launch = json.loads(record.read_text())
    assert launch == {"argv": ["stdio", "--read-only", "--toolsets", "repos"], "token": TOKEN}


_STATUS = "successfully downloaded text file (SHA: abc123)"


def _read_file(tmp_path: Path, *paths: str) -> list[str]:
    """Call ``get_file_contents`` on the fake server once for each path.

    Args:
        tmp_path: Directory for the fake server.
        *paths: The paths to read.

    Returns:
        The results, in order.
    """
    binary, _ = _fake_binary(tmp_path)

    async def run() -> list[str]:
        """Read each path.

        Returns:
            The results.
        """
        async with github_repo_tools(str(binary), TOKEN) as tools:
            read = next(tool for tool in tools if tool.name == "get_file_contents")
            return [await read.acall(owner="o", repo="r", path=path) for path in paths]

    return asyncio.run(run())


def test_file_read_gives_the_model_the_file_body(tmp_path: Path) -> None:
    """The body that the server sends as an embedded resource reaches the model after the status line."""
    (small,) = _read_file(tmp_path, "README.md")
    assert small == f"{_STATUS}\n\ncontents"


def test_long_file_bodies_and_part_lists_are_cut_with_a_clear_marker(tmp_path: Path) -> None:
    """The cap applies to the whole result: a long embedded file body, and several text parts together."""
    part = MAX_RESULT_CHARS // 2
    big, parts = _read_file(tmp_path, str(MAX_RESULT_CHARS + 500), f"parts:{part}")
    whole = len(_STATUS) + 2 + MAX_RESULT_CHARS + 500
    assert big.startswith(f"{_STATUS}\n\n" + "x" * (MAX_RESULT_CHARS - len(_STATUS) - 2) + "\n\n[Truncated:")
    assert f"has {whole} characters" in big
    assert len(big) < MAX_RESULT_CHARS + 300
    assert parts.startswith("p" * part + "\n\n" + "p" * (MAX_RESULT_CHARS - part - 2) + "\n\n[Truncated:")
    assert f"has {3 * part + 4} characters" in parts


@pytest.mark.skipif(shutil.which("github-mcp-server") is None, reason="github-mcp-server is not installed")
def test_real_server_offers_only_the_allow_listed_read_tools() -> None:
    """The pinned server, started read-only, lists exactly the allow-listed read tools."""

    async def run() -> list[str]:
        """List the real server's tools without calling one.

        Returns:
            The tool names.
        """
        async with github_repo_tools("github-mcp-server", TOKEN) as tools:
            return _names(tools)

    assert asyncio.run(run()) == sorted(READ_TOOLS)


def _patched_tools(monkeypatch: pytest.MonkeyPatch, opened: list[str | None]) -> None:
    """Replace the GitHub server with one fake tool and record each token it opens with.

    Args:
        monkeypatch: Pytest's monkeypatch fixture.
        opened: Receives the token of each launch.
    """

    @asynccontextmanager
    async def fake(_binary: str, token: str | None) -> AsyncGenerator[list[dspy.Tool]]:
        """Yield one fake read tool."""
        opened.append(token)
        yield [dspy.Tool(lambda query: query, name="search_repositories")]

    monkeypatch.setattr(generalist, "github_repo_tools", fake)


@pytest.mark.parametrize(
    ("state", "allowed", "token", "offered"),
    [
        ({"repo_provider": "github"}, {generalist.GITHUB_REPOS_TOOL}, TOKEN, True),
        ({}, {generalist.GITHUB_REPOS_TOOL}, TOKEN, True),
        ({"repo_provider": "gitlab"}, {generalist.GITHUB_REPOS_TOOL}, TOKEN, False),
        ({"repo_provider": "github"}, set(), TOKEN, False),
        ({"repo_provider": "github"}, {generalist.GITHUB_REPOS_TOOL}, None, False),
    ],
    ids=["github", "no-provider", "other-host", "other-screen", "not-linked"],
)
def test_setup_turn_offers_github_tools_only_with_a_github_link(
    monkeypatch: pytest.MonkeyPatch, state: dict, allowed: set[str], token: str | None, offered: bool
) -> None:
    """The setup turn opens the GitHub tools with the repository listing, for GitHub, with a token."""
    opened: list[str | None] = []
    _patched_tools(monkeypatch, opened)

    async def run() -> list[str]:
        """Collect the tool names one turn gets.

        Returns:
            The tool names.
        """
        async with generalist.AsyncExitStack() as stack:
            tools = await generalist._github_repo_tools(stack, state, allowed, lambda: token)
            return _names(tools)

    names = asyncio.run(run())
    assert names == (["search_repositories"] if offered else [])
    assert opened == ([TOKEN] if offered else [])


def test_setup_turn_uses_the_configured_binary(monkeypatch: pytest.MonkeyPatch) -> None:
    """The turn starts the executable the settings name."""
    seen: list[str] = []

    @asynccontextmanager
    async def fake(binary: str, _token: str | None) -> AsyncGenerator[list[dspy.Tool]]:
        """Record the executable."""
        seen.append(binary)
        yield []

    monkeypatch.setattr(generalist, "github_repo_tools", fake)
    monkeypatch.setattr(generalist.settings, "github_mcp_binary", "/opt/bin/github-mcp-server")

    async def run() -> None:
        """Open the tools for a GitHub repository screen."""
        async with generalist.AsyncExitStack() as stack:
            await generalist._github_repo_tools(stack, {}, {generalist.GITHUB_REPOS_TOOL}, lambda: TOKEN)

    asyncio.run(run())
    assert seen == ["/opt/bin/github-mcp-server"]
