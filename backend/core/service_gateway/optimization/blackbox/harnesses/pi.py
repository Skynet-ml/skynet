"""Pi harness (``@earendil-works/pi-coding-agent``) and the ``models.json`` Pi and its forks read."""

from __future__ import annotations

import json
from typing import Any

from .....models.blackbox import BLACKBOX_HARNESS_PI
from ..harness_bridge import parse_pi_output
from . import HarnessSpec, register
from .base import (
    _GUARD_PI_ARG,
    _PROMPT_ARG,
    ENV_API_KEY,
    ENV_MODEL,
    PROVIDER,
    GatewayConfig,
    HarnessLaunch,
    _base_env,
)

PI_PACKAGE = "@earendil-works/pi-coding-agent"
PI_VERSION = "0.87.1"
PI_THINKING_LEVEL = "low"
# Skynet's effort names that the CLIs spell differently; others pass through.
_PI_THINKING_LEVELS = {"none": "off"}
# Every Skynet level has a Pi 0.87.1 thinking level (``VALID_THINKING_LEVELS``:
# off, minimal, low, medium, high, xhigh, max), and the ``thinkingLevelMap``
# entry below keeps Pi from clamping ``xhigh`` or ``max``. Prime Agent 0.9.7
# bundles the same list and clamp, so it declares these levels too.
PI_EFFORTS = ("none", "minimal", "low", "medium", "high", "xhigh", "max")
# The sandbox image ships its own pi in /usr/local/bin. A pinned copy lives in
# a private prefix put ahead of it on PATH, so the version the transcript logs
# is the one that ran.
_PI_PREFIX = '"$HOME/.app/pi"'
_PI_PATH = 'export PATH="$HOME/.app/pi/bin:$PATH"'


def _pi_install() -> str:
    """Return the command that pins pi to :data:`PI_VERSION` and logs the version that will run.

    The image's own pi is kept when it already is the pinned version, so the
    install is free on the common path.

    Returns:
        The shell command.
    """
    return (
        f'{_PI_PATH}; [ "$(pi --version 2>/dev/null)" = "{PI_VERSION}" ] '
        f"|| npm install -g --prefix {_PI_PREFIX} {PI_PACKAGE}@{PI_VERSION} >/dev/null && pi --version"
    )


def _pi_models_json(
    model: str, gateway: GatewayConfig, *, reasoning_effort: str | None = None
) -> tuple[dict[str, Any], str]:
    """Build Pi's ``models.json`` for the gateway and the ``--model`` argument that selects the entry.

    Prime Agent is a Pi fork and reads the same file.

    Args:
        model: Target model id.
        gateway: Gateway URL and key.
        reasoning_effort: The user's effort for the model, or ``None`` for the default.

    Returns:
        The ``models.json`` content and the quoted ``--model`` argument.
    """
    openrouter = "openrouter.ai" in gateway.url
    # An explicit effort comes from a model the catalog lists as reasoning-capable,
    # so its gateway accepts the field the default leaves out.
    reasoning = openrouter or bool(reasoning_effort)
    level = _PI_THINKING_LEVELS.get(reasoning_effort, reasoning_effort) if reasoning_effort else PI_THINKING_LEVEL
    entry: dict[str, object] = {
        "id": model,
        "name": model,
        # OpenRouter models such as DeepSeek reason by default and, left to that default, think until
        # they hit ``maxTokens`` without a single tool call or answer. Pi's OpenRouter thinking format
        # makes every request carry an explicit effort instead; other gateways may reject that field.
        "reasoning": reasoning,
        "input": ["text"],
        "contextWindow": 200_000,
        "maxTokens": 32_000,
        "cost": {"input": 0, "output": 0, "cacheRead": 0, "cacheWrite": 0},
    }
    if openrouter:
        entry["compat"] = {"thinkingFormat": "openrouter"}
    if reasoning_effort:
        # Without a map entry Pi sends no effort for ``off`` on an OpenAI-style gateway, so the model
        # keeps its default thinking, and it clamps ``xhigh`` and ``max`` down to ``high``.
        entry["thinkingLevelMap"] = {level: reasoning_effort}
    model_arg = f'"${ENV_MODEL}:{level}"' if reasoning else f'"${ENV_MODEL}"'
    models_json = {
        "providers": {
            PROVIDER: {
                "baseUrl": gateway.url,
                "api": "openai-completions",
                # Pi reads a bare env-var name as the literal key; the shell form is honoured.
                "apiKey": f"!printenv {ENV_API_KEY}",
                "models": [entry],
            }
        }
    }
    return models_json, model_arg


def _pi_launch(model: str, gateway: GatewayConfig, *, reasoning_effort: str | None = None) -> HarnessLaunch:
    """Build the launch for Pi (:data:`PI_PACKAGE`, pinned to :data:`PI_VERSION`).

    Args:
        model: Target model id.
        gateway: Gateway URL and key.
        reasoning_effort: The user's effort for the model, or ``None`` for the default.

    Returns:
        The launch.
    """
    models_json, model_arg = _pi_models_json(model, gateway, reasoning_effort=reasoning_effort)
    return HarnessLaunch(
        instructions_file="AGENTS.md",
        install_command=_pi_install(),
        files={".app/pi/models.json": json.dumps(models_json, indent=2)},
        run_command=(
            f'{_PI_PATH}; mkdir -p "$HOME/.pi/agent" && cp .app/pi/models.json "$HOME/.pi/agent/models.json" && '
            f"pi --mode json --no-session --provider {PROVIDER} --model {model_arg} {_GUARD_PI_ARG}{_PROMPT_ARG}"
        ),
        env=_base_env(model, gateway),
        parse_output=parse_pi_output,
    )


SPEC = register(
    HarnessSpec(
        id=BLACKBOX_HARNESS_PI,
        launch=_pi_launch,
        binary="pi",
        version=PI_VERSION,
        output_format="pi",
        position=10,
        supported_efforts=PI_EFFORTS,
    )
)
