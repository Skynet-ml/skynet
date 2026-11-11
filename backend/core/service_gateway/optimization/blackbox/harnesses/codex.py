"""Codex CLI harness (``@openai/codex``)."""

from __future__ import annotations

import json

from .....exceptions import ServiceError
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
# The named ``ReasoningEffort`` levels that Codex 0.153.0 sends unchanged
# (codex-rs/protocol/src/openai_models.rs). ``ultra`` and ``persistent`` are
# left out: ``reasoning_effort_for_request`` in codex-rs/core/src/client.rs
# swaps them for another level or for ``disabled``.
CODEX_EFFORTS = ("none", "minimal", "low", "medium", "high", "xhigh", "max")


def _codex_launch(model: str, gateway: GatewayConfig, *, reasoning_effort: str | None = None) -> HarnessLaunch:
    """Build the launch for Codex CLI (``@openai/codex``).

    Args:
        model: Target model id.
        gateway: Gateway URL and key.
        reasoning_effort: The user's effort for the model, or ``None`` for Codex's default.

    Returns:
        The launch.

    Raises:
        ServiceError: When ``reasoning_effort`` is not in :data:`CODEX_EFFORTS`.
    """
    if reasoning_effort and reasoning_effort not in CODEX_EFFORTS:
        raise ServiceError(
            f"The Codex harness does not support the effort level {reasoning_effort!r}. "
            f"Supported levels: {', '.join(CODEX_EFFORTS)}."
        )
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
            # The sandbox run still sent web_search with only the config setting
            # (SKY-529 check 8); a -c override wins over every config layer.
            "-c 'web_search=\"disabled\"' -c features.multi_agent=false "
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
        supported_efforts=CODEX_EFFORTS,
    )
)
