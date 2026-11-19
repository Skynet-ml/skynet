"""Harness catalog: how each coding agent is installed, pointed at the gateway and run.

A harness is the program that turns a model into a coding agent — Pi,
Codex, OpenCode, Prime Agent, or a user-supplied command. Every entry
knows the file its instructions live in (that file is the text under
optimization), how to install itself, the config that routes its model
calls to Skynet's gateway, the command that runs one case headlessly, and
how to read the agent's final answer and token usage out of its output.
Model routing is gateway-only: a sandbox sees the run's gateway key, never
a provider key. Each built-in CLI lives in its own :mod:`.harnesses` module;
this module keeps the MCP harness list and re-exports the old names.
"""

from __future__ import annotations

from ....models.blackbox import (
    BLACKBOX_HARNESS_ANTIGRAVITY_SDK,
    BLACKBOX_HARNESS_CLINE,
    BLACKBOX_HARNESS_CODE_PUPPY,
    BLACKBOX_HARNESS_CODEX,
    BLACKBOX_HARNESS_DEERFLOW,
    BLACKBOX_HARNESS_EVE,
    BLACKBOX_HARNESS_FABRIC,
    BLACKBOX_HARNESS_GOOSE,
    BLACKBOX_HARNESS_GROK_BUILD,
    BLACKBOX_HARNESS_HERMES,
    BLACKBOX_HARNESS_KIMI_CODE,
    BLACKBOX_HARNESS_OPENCODE,
    BLACKBOX_HARNESS_OPENCODE_V2,
    BLACKBOX_HARNESS_OPENHANDS,
    BLACKBOX_HARNESS_OPENHANDS_SDK,
    BLACKBOX_HARNESS_QWEN_CODE,
)
from .harnesses.base import _GUARD_CODEX_COPY as _GUARD_CODEX_COPY
from .harnesses.base import _GUARD_PI_ARG as _GUARD_PI_ARG
from .harnesses.base import _PROMPT_ARG as _PROMPT_ARG
from .harnesses.base import ANSWER_FILE as ANSWER_FILE
from .harnesses.base import ENV_ANSWER_FILE as ENV_ANSWER_FILE
from .harnesses.base import ENV_API_KEY as ENV_API_KEY
from .harnesses.base import ENV_GATEWAY_URL as ENV_GATEWAY_URL
from .harnesses.base import ENV_MODEL as ENV_MODEL
from .harnesses.base import ENV_PROMPT_FILE as ENV_PROMPT_FILE
from .harnesses.base import PROMPT_FILE as PROMPT_FILE
from .harnesses.base import PROVIDER as PROVIDER
from .harnesses.base import GatewayConfig as GatewayConfig
from .harnesses.base import HarnessLaunch as HarnessLaunch
from .harnesses.base import OutputParser as OutputParser
from .harnesses.base import Usage as Usage
from .harnesses.base import _base_env as _base_env
from .harnesses.base import _npm_install as _npm_install
from .harnesses.base import _version_check as _version_check
from .harnesses.codex import CODEX_VERSION as CODEX_VERSION
from .harnesses.codex import _codex_launch as _codex_launch
from .harnesses.opencode import OPENCODE_VERSION as OPENCODE_VERSION
from .harnesses.opencode import _opencode_launch as _opencode_launch
from .harnesses.pi import _PI_PATH as _PI_PATH
from .harnesses.pi import _PI_PREFIX as _PI_PREFIX
from .harnesses.pi import _PI_THINKING_LEVELS as _PI_THINKING_LEVELS
from .harnesses.pi import PI_PACKAGE as PI_PACKAGE
from .harnesses.pi import PI_THINKING_LEVEL as PI_THINKING_LEVEL
from .harnesses.pi import PI_VERSION as PI_VERSION
from .harnesses.pi import _pi_install as _pi_install
from .harnesses.pi import _pi_launch as _pi_launch
from .harnesses.pi import _pi_models_json as _pi_models_json
from .harnesses.prime import _PRIME_AGENT_DIR as _PRIME_AGENT_DIR
from .harnesses.prime import PRIME_AGENT_IMAGE_KERNEL as PRIME_AGENT_IMAGE_KERNEL
from .harnesses.prime import PRIME_AGENT_TARBALL as PRIME_AGENT_TARBALL
from .harnesses.prime import PRIME_AGENT_VERSION as PRIME_AGENT_VERSION
from .harnesses.prime import _prime_agent_install as _prime_agent_install
from .harnesses.prime import _prime_agent_launch as _prime_agent_launch

# The catalog harnesses that load the [proposer] MCP servers from the relay
# urls in their generated config. Any other harness would run without the
# tools the file lists, so its launch stops (SKY-610, SKY-618, SKY-619, SKY-620, SKY-621).
MCP_HARNESSES = (
    BLACKBOX_HARNESS_CODEX,
    BLACKBOX_HARNESS_OPENCODE,
    BLACKBOX_HARNESS_OPENCODE_V2,
    BLACKBOX_HARNESS_QWEN_CODE,
    BLACKBOX_HARNESS_CLINE,
    BLACKBOX_HARNESS_KIMI_CODE,
    BLACKBOX_HARNESS_OPENHANDS,
    BLACKBOX_HARNESS_HERMES,
    BLACKBOX_HARNESS_GOOSE,
    BLACKBOX_HARNESS_OPENHANDS_SDK,
    BLACKBOX_HARNESS_ANTIGRAVITY_SDK,
    BLACKBOX_HARNESS_DEERFLOW,
    BLACKBOX_HARNESS_EVE,
    BLACKBOX_HARNESS_FABRIC,
    BLACKBOX_HARNESS_CODE_PUPPY,
    BLACKBOX_HARNESS_GROK_BUILD,
)


def mcp_unsupported(name: str) -> str:
    """Explain that a proposer cannot load the ``[proposer] mcp`` servers.

    Args:
        name: The proposer, such as ``custom`` or ``host CLI codex``.

    Returns:
        The message, which names the cause and the fix.
    """
    return (
        f"The {name} proposer harness cannot load the [proposer] mcp servers yet. "
        f"Use one of these harnesses: {', '.join(MCP_HARNESSES)}, "
        "or remove [proposer] mcp from optimize.toml."
    )
