"""Gemini CLI harness (``gemini``).

Adapted from Harbor 0.25.0 (Apache-2.0), agents/installed/gemini_cli: the npm
install, the ``--yolo``/``--model``/``--prompt`` one-shot run with stdin
closed, the ``gemini-api-key`` auth type in ``settings.json``, and the
settings alias that carries the reasoning effort as a thinking level. Harbor
sends the run to Google with a real ``GEMINI_API_KEY``; Skynet pins the
release, points ``GOOGLE_GEMINI_BASE_URL`` at the model gateway and passes
the gateway token as the key instead.
"""

from __future__ import annotations

import json
import shlex

from .....models.blackbox import BLACKBOX_HARNESS_GEMINI_CLI
from ..harness_bridge import parse_plain_output
from . import HarnessSpec, register
from .base import _PROMPT_ARG, ENV_API_KEY, ENV_MODEL, GatewayConfig, HarnessLaunch, _base_env, _npm_install

# npm registry ``dist-tags.latest`` for ``@google/gemini-cli`` on 2026-11-08 (Apache-2.0).
GEMINI_CLI_VERSION = "0.67.0"
# The gateway URL lives in this file rather than on the command line, so a
# protected run's relay rewrite of generated files reaches it.
GEMINI_BASE_URL_FILE = ".app/gemini/base_url"
# Gemini's thinking levels; Skynet's larger efforts cap at ``high``, and any
# other effort gets no alias, so Gemini keeps its default.
_GEMINI_THINKING_LEVELS = {"minimal": "MINIMAL", "low": "LOW", "medium": "MEDIUM", "high": "HIGH"}
_GEMINI_THINKING_ALIASES = {"xhigh": "high", "max": "high"}
# Gemini reads these for its Vertex, Code Assist and Google-key routes. A
# protected run blanks or turns them off, so an inherited credential never
# gives Gemini a route around the gateway.
_PROTECTED_ENV = {
    "GOOGLE_API_KEY": "",
    "GOOGLE_APPLICATION_CREDENTIALS": "",
    "GOOGLE_CLOUD_PROJECT": "",
    "GOOGLE_GENAI_USE_VERTEXAI": "false",
    "GOOGLE_GENAI_USE_GCA": "false",
    "GEMINI_TELEMETRY_ENABLED": "false",
    "DO_NOT_TRACK": "1",
}


def _gemini_thinking_level(reasoning_effort: str | None) -> str | None:
    """Return Gemini's thinking level for the user's effort, or ``None`` for Gemini's default.

    Args:
        reasoning_effort: The user's effort for the model, or ``None``.

    Returns:
        The thinking level, or ``None``.
    """
    if not reasoning_effort:
        return None
    return _GEMINI_THINKING_LEVELS.get(_GEMINI_THINKING_ALIASES.get(reasoning_effort, reasoning_effort))


def _gemini_settings(model: str, alias: str | None, thinking_level: str | None) -> str:
    """Build Gemini's ``settings.json``: API-key auth, no auto-update, no telemetry, and the effort alias.

    Args:
        model: Target model id.
        alias: The settings alias that carries the thinking level, or ``None``.
        thinking_level: Gemini's thinking level for the alias, or ``None``.

    Returns:
        The settings as JSON text.
    """
    settings: dict[str, object] = {
        # Headless runs need an explicit auth type; autodetection fails with a custom base URL.
        "security": {"auth": {"selectedType": "gemini-api-key"}},
        "general": {"enableAutoUpdate": False, "enableAutoUpdateNotification": False},
        "privacy": {"usageStatisticsEnabled": False},
        "telemetry": {"enabled": False},
    }
    if alias and thinking_level:
        settings["modelConfigs"] = {
            "customAliases": {
                alias: {
                    "modelConfig": {
                        "model": model,
                        "generateContentConfig": {
                            "thinkingConfig": {"includeThoughts": True, "thinkingLevel": thinking_level}
                        },
                    }
                }
            }
        }
    return json.dumps(settings, sort_keys=True)


def _gemini_launch(model: str, gateway: GatewayConfig, *, reasoning_effort: str | None = None) -> HarnessLaunch:
    """Build the launch for Gemini CLI (``gemini``, pinned to :data:`GEMINI_CLI_VERSION`).

    Each run gets a fresh Gemini home outside the workspace, so Gemini's
    state and logs never land in the files the run hands back. The settings
    go there from the command, not as a generated file, because the relay
    rewrite expects each generated file to carry the gateway URL. The gateway
    token goes to ``GEMINI_API_KEY`` on the command itself, so it replaces
    any inherited key. Gemini's plain one-shot output is the answer.

    Args:
        model: Target model id.
        gateway: Gateway URL and key.
        reasoning_effort: The user's effort for the model, or ``None`` for Gemini's default.

    Returns:
        The launch.
    """
    thinking_level = _gemini_thinking_level(reasoning_effort)
    alias = f"skynet-{thinking_level.lower()}" if thinking_level else None
    model_arg = alias or f'"${ENV_MODEL}"'
    settings = _gemini_settings(model, alias, thinking_level)
    run_command = (
        'export GEMINI_CLI_HOME="$(mktemp -d)" && '
        'mkdir -p "$GEMINI_CLI_HOME/.gemini" && '
        f'printf %s {shlex.quote(settings)} > "$GEMINI_CLI_HOME/.gemini/settings.json" && '
        "GEMINI_CLI_TRUST_WORKSPACE=true "
        f'GOOGLE_GEMINI_BASE_URL="$(cat {GEMINI_BASE_URL_FILE})" '
        f'GEMINI_API_KEY="${ENV_API_KEY}" '
        f"gemini --yolo --model={model_arg} --prompt={_PROMPT_ARG} < /dev/null"
    )
    return HarnessLaunch(
        instructions_file="GEMINI.md",
        install_command=_npm_install("gemini", "@google/gemini-cli", GEMINI_CLI_VERSION),
        files={GEMINI_BASE_URL_FILE: gateway.url},
        run_command=run_command,
        env=_base_env(model, gateway),
        parse_output=parse_plain_output,
    )


SPEC = register(
    HarnessSpec(
        id=BLACKBOX_HARNESS_GEMINI_CLI,
        launch=_gemini_launch,
        binary="gemini",
        version=GEMINI_CLI_VERSION,
        output_format="plain",
        protected_env=_PROTECTED_ENV,
        position=63,
    )
)
