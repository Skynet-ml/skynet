"""Give an agent turn the read-only repository tools of the official GitHub MCP server.

The server is the self-hosted ``github-mcp-server`` binary in stdio mode,
started with ``--read-only --toolsets repos``. It authenticates with the
caller's linked GitHub token from the connector vault. The token goes only
into the child's environment, never into its argv or a log.
"""

from __future__ import annotations

import os
import shutil
from collections.abc import AsyncGenerator, Iterable
from contextlib import asynccontextmanager
from datetime import timedelta
from pathlib import Path
from typing import Any

import dspy
from mcp import ClientSession
from mcp.client.stdio import StdioServerParameters, stdio_client
from mcp.types import Tool

GITHUB_MCP_VERSION = "2.1.0"
TOKEN_ENV = "GITHUB_PERSONAL_ACCESS_TOKEN"
SERVER_ARGS = ("stdio", "--read-only", "--toolsets", "repos")
CALL_TIMEOUT_SECONDS = 60
# get_file_contents and search_code can return megabytes, and every byte goes
# back into the model's prompt for the rest of the turn.
MAX_RESULT_CHARS = 40_000
# The repos toolset also has read tools the setup agent does not need, such
# as list_repository_collaborators, so only these reach the agent.
READ_TOOLS = frozenset(
    {
        "search_repositories",
        "search_code",
        "search_commits",
        "get_file_contents",
        "list_branches",
        "list_commits",
        "get_commit",
        "list_tags",
        "get_tag",
        "list_releases",
        "get_latest_release",
        "get_release_by_tag",
    }
)


class GitHubMcpError(RuntimeError):
    """Raised when the GitHub repository tools cannot start."""


def server_parameters(command: str, token: str) -> StdioServerParameters:
    """Build the stdio launch for the read-only GitHub MCP server.

    Args:
        command: Path of the ``github-mcp-server`` executable.
        token: The caller's GitHub token.

    Returns:
        The launch, with the token only in the environment.
    """
    return StdioServerParameters(command=command, args=list(SERVER_ARGS), env={TOKEN_ENV: token})


def read_only_tools(tools: Iterable[Tool]) -> list[Tool]:
    """Keep the allow-listed tools that the server marks read-only.

    ``--read-only`` already drops the write tools. The annotation check is a
    second guard in case a server release changes that flag.

    Args:
        tools: The server's tool listing.

    Returns:
        The tools the agent may call.
    """
    return [
        tool
        for tool in tools
        if tool.name in READ_TOOLS and tool.annotations is not None and tool.annotations.readOnlyHint is True
    ]


def cap_result(result: Any) -> Any:
    """Cut a long text tool result to :data:`MAX_RESULT_CHARS` and say so.

    Args:
        result: One tool result.

    Returns:
        The result, with a truncation note when the text was cut.
    """
    if not isinstance(result, str) or len(result) <= MAX_RESULT_CHARS:
        return result
    return (
        f"{result[:MAX_RESULT_CHARS]}\n\n[Truncated: the result has {len(result)} characters and only the first "
        f"{MAX_RESULT_CHARS} are shown. Ask for a narrower path, file or query to see the rest.]"
    )


def _capped(tool: dspy.Tool) -> dspy.Tool:
    """Make the tool return results capped by :func:`cap_result`.

    Args:
        tool: A tool built from the MCP listing.

    Returns:
        The same tool.
    """
    call = tool.func

    async def func(**kwargs: Any) -> Any:
        """Call the MCP tool and cap its result.

        Args:
            **kwargs: The tool's arguments.

        Returns:
            The capped result.
        """
        return cap_result(await call(**kwargs))

    tool.func = func
    return tool


@asynccontextmanager
async def github_repo_tools(binary: str, token: str | None) -> AsyncGenerator[list[dspy.Tool]]:
    """Start the GitHub MCP server for one turn and yield its read-only repository tools.

    Args:
        binary: Name or path of the ``github-mcp-server`` executable.
        token: The caller's linked GitHub token, or ``None`` when GitHub is
            not linked.

    Yields:
        The read-only tools, bound to a session that closes with the context.
        Their text results are capped by :func:`cap_result`.

    Raises:
        GitHubMcpError: When the token is missing or the executable is not
            installed.
    """
    if not token:
        raise GitHubMcpError("GitHub is not linked to this account; press Connect GitHub to link it")
    command = shutil.which(binary)
    if command is None:
        raise GitHubMcpError(
            f"The GitHub MCP server executable {binary!r} was not found; install github-mcp-server {GITHUB_MCP_VERSION}"
        )
    # The server's stderr can echo request details, so it never reaches the
    # backend log. Opening /dev/null does not block the loop.
    with Path(os.devnull).open("w") as errlog:  # noqa: ASYNC230
        async with (
            stdio_client(server_parameters(command, token), errlog=errlog) as (read, write),
            ClientSession(read, write, read_timeout_seconds=timedelta(seconds=CALL_TIMEOUT_SECONDS)) as session,
        ):
            await session.initialize()
            listing = await session.list_tools()
            yield [_capped(dspy.Tool.from_mcp_tool(session, spec)) for spec in read_only_tools(listing.tools)]
