"""Codex CLI harness (``@openai/codex``)."""

from __future__ import annotations

import json

from .....models.blackbox import BLACKBOX_HARNESS_CODEX
from ..harness_bridge import parse_codex_output
from . import HarnessSpec, register
from .base import (
    _GUARD_CODEX_COPY,
    _PROMPT_ARG,
    ENV_API_KEY,
    PROVIDER,
    GatewayConfig,
    HarnessLaunch,
    _base_env,
    _npm_install,
)

CODEX_VERSION = "0.153.0"


def _codex_launch(model: str, gateway: GatewayConfig, *, reasoning_effort: str | None = None) -> HarnessLaunch:
    """Build the launch for Codex CLI (``@openai/codex``).

    Args:
        model: Target model id.
        gateway: Gateway URL and key.
        reasoning_effort: The user's effort for the model, or ``None`` for Codex's default.

    Returns:
        The launch.
    """
    # The gateway prices only function and custom tools; without these settings
    # Codex also sends tool_search and web_search tools, and the gateway rejects them.
    config_toml = (
        f"model = {json.dumps(model)}\n"
        f'model_provider = "{PROVIDER}"\n'
        + (f"model_reasoning_effort = {json.dumps(reasoning_effort)}\n" if reasoning_effort else "")
        + 'web_search = "disabled"\n'
        "\n"
        f"[model_providers.{PROVIDER}]\n"
        'name = "Model gateway"\n'
        f"base_url = {json.dumps(gateway.url)}\n"
        f'env_key = "{ENV_API_KEY}"\n'
        'wire_api = "responses"\n'
        "request_max_retries = 0\n"
        "stream_max_retries = 0\n"
        "supports_websockets = false\n"
        "\n"
        "[features]\n"
        "multi_agent = false\n"
    )
    return HarnessLaunch(
        instructions_file="AGENTS.md",
        install_command=_npm_install("codex", "@openai/codex", CODEX_VERSION),
        files={".app/codex/config.toml": config_toml},
        run_command=(
            f"{_GUARD_CODEX_COPY}"
            # Codex skips a new hooks.json hook until it is trusted, so the guard
            # needs the trust flag; without the guard no repository hook gets it.
            'CODEX_HOME="$PWD/.app/codex" codex exec --skip-git-repo-check '
            "${APP_GUARD_CODEX:+--dangerously-bypass-hook-trust} "
            f"--dangerously-bypass-approvals-and-sandbox --json {_PROMPT_ARG}"
        ),
        env=_base_env(model, gateway),
        parse_output=parse_codex_output,
    )


SPEC = register(
    HarnessSpec(
        id=BLACKBOX_HARNESS_CODEX,
        launch=_codex_launch,
        binary="codex",
        version=CODEX_VERSION,
        output_format="codex",
        position=20,
    )
)
