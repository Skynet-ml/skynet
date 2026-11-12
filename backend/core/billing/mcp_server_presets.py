"""List the remote MCP servers a user can connect with one click.

Each preset is a public streamable-HTTP MCP endpoint whose authorization
server supports dynamic client registration, so Skynet needs no pre-registered
client. The user's own account at the service pays for its tools. A user can
also connect any other MCP server by its URL.
"""

from __future__ import annotations

from dataclasses import dataclass


@dataclass(frozen=True)
class McpServerPreset:
    """One remote MCP server in the picker."""

    name: str
    title: str
    url: str


PRESETS: dict[str, McpServerPreset] = {
    preset.name: preset
    for preset in (
        McpServerPreset("linear", "Linear", "https://mcp.linear.app/mcp"),
        McpServerPreset("notion", "Notion", "https://mcp.notion.com/mcp"),
        McpServerPreset("sentry", "Sentry", "https://mcp.sentry.dev/mcp"),
        McpServerPreset("stripe", "Stripe", "https://mcp.stripe.com"),
        McpServerPreset("vercel", "Vercel", "https://mcp.vercel.com"),
        McpServerPreset("huggingface", "Hugging Face", "https://huggingface.co/mcp"),
    )
}
