"""OpenCode harness (``opencode-ai``)."""

from __future__ import annotations

import json
from urllib.parse import quote

from .....models.blackbox import BLACKBOX_HARNESS_OPENCODE
from ..harness_bridge import parse_opencode_output
from . import HarnessSpec, register
from .base import (
    _PROMPT_ARG,
    ENV_API_KEY,
    ENV_MODEL,
    PROVIDER,
    GatewayConfig,
    HarnessLaunch,
    _base_env,
    _npm_install,
)

OPENCODE_VERSION = "1.18.27"
# A protected sandbox has no route to the internet, so OpenCode must not try
# to update itself or fetch models, default plugins or language servers.
_PROTECTED_ENV = {
    "OPENCODE_DISABLE_AUTOUPDATE": "true",
    "OPENCODE_DISABLE_MODELS_FETCH": "true",
    "OPENCODE_DISABLE_DEFAULT_PLUGINS": "true",
    "OPENCODE_DISABLE_LSP_DOWNLOAD": "true",
}


def _opencode_launch(
    model: str,
    gateway: GatewayConfig,
    *,
    reasoning_effort: str | None = None,
    mcp_servers: tuple[str, ...] = (),
) -> HarnessLaunch:
    """Build the launch for OpenCode (``opencode-ai``).

    Args:
        model: Target model id.
        gateway: Gateway URL and key.
        reasoning_effort: The user's effort for the model, or ``None`` for OpenCode's default.
        mcp_servers: The ``[proposer]`` MCP server names. Each gets the gateway's
            ``/mcp/<name>`` url, which the box's proxy relays with its own token,
            so OpenCode never holds the real url or a secret (SKY-618).

    Returns:
        The launch.
    """
    entry: dict[str, object] = {"name": model}
    if reasoning_effort:
        # OpenCode hands model options to its bundled @ai-sdk/openai-compatible as
        # provider options, which sends this one as ``reasoning_effort``.
        entry["options"] = {"reasoningEffort": reasoning_effort}
    config = {
        "$schema": "https://opencode.ai/config.json",
        "provider": {
            PROVIDER: {
                "npm": "@ai-sdk/openai-compatible",
                "name": "Model gateway",
                "options": {"baseURL": gateway.url, "apiKey": f"{{env:{ENV_API_KEY}}}"},
                "models": {model: entry},
            }
        },
        "model": f"{PROVIDER}/{model}",
    }
    if mcp_servers:
        # The relay needs no sign-in, so ``oauth: false`` stops OpenCode from
        # starting an OAuth flow when a server answers 401.
        config["mcp"] = {
            name: {
                "type": "remote",
                "url": f"{gateway.url}/mcp/{quote(name, safe='')}",
                "enabled": True,
                "oauth": False,
            }
            for name in mcp_servers
        }
    return HarnessLaunch(
        instructions_file="AGENTS.md",
        install_command=_npm_install("opencode", "opencode-ai", OPENCODE_VERSION),
        files={"opencode.json": json.dumps(config, indent=2)},
        run_command=f'opencode run --format json --model "{PROVIDER}/${ENV_MODEL}" {_PROMPT_ARG}',
        env=_base_env(model, gateway),
        parse_output=parse_opencode_output,
    )


SPEC = register(
    HarnessSpec(
        id=BLACKBOX_HARNESS_OPENCODE,
        launch=_opencode_launch,
        binary="opencode",
        version=OPENCODE_VERSION,
        output_format="opencode",
        protected_env=_PROTECTED_ENV,
        position=30,
    )
)
