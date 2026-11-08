"""Aider harness (``aider-chat``).

Adapted from Harbor 0.25.0 (Apache-2.0), agents/installed/aider.py: the
install through uv, the scripting-mode run and the workspace snapshot.
Skynet pins the release, points Aider at the model gateway through a config
file, and keeps its own plain transcript format.
"""

from __future__ import annotations

import json

from ..harness_bridge import parse_plain_output
from . import HarnessSpec, register
from .base import ENV_PROMPT_FILE, GatewayConfig, HarnessLaunch, _base_env, _version_check

AIDER_ID = "aider"
AIDER_PACKAGE = "aider-chat"
AIDER_VERSION = "0.86.2"
# The pinned uv release installs the pinned Aider; ``aider-chat`` needs Python < 3.13.
AIDER_UV_VERSION = "0.13.7"
AIDER_PYTHON = "3.12"
AIDER_CONFIG_FILE = ".app/aider/aider.conf.yml"
# Aider runs on LiteLLM, which routes an ``openai/`` model to the OpenAI-style
# base URL in the config file; the gateway receives the id after the prefix.
_LITELLM_PREFIX = "openai/"
# ``uv tool install`` and the uv installer both put their binaries here.
_AIDER_PATH = 'export PATH="$HOME/.local/bin:$PATH"'
# Aider sees only files a git repository tracks, so a plain workspace is
# committed once before the run, as Harbor does. Harbor bounds ``git add``
# with ``timeout``; an image without coreutils' ``timeout`` runs it unbounded.
_SNAPSHOT_UNTRACKED_WORKSPACE = (
    "if command -v git >/dev/null 2>&1 "
    "&& ! git rev-parse --is-inside-work-tree >/dev/null 2>&1; then "
    "_aider_bound=; command -v timeout >/dev/null 2>&1 && _aider_bound='timeout 120'; "
    "{ git init -q && $_aider_bound git add -A "
    "&& git -c user.name=Agent -c user.email=agent@localhost "
    "commit -q --no-verify --allow-empty -m 'Task files before agent run'; } "
    "|| echo 'agent: could not snapshot the workspace for aider' >&2; "
    "fi; "
)
# Every Aider option also reads an ``AIDER_*`` variable, so these hold even
# when the run replaces the built-in command or config. A protected sandbox has
# no route to the internet: no analytics, update check, release notes or URL
# scraping, and LiteLLM keeps its bundled cost map instead of fetching one.
_PROTECTED_ENV = {
    "AIDER_ANALYTICS": "false",
    "AIDER_ANALYTICS_DISABLE": "true",
    "AIDER_CHECK_UPDATE": "false",
    "AIDER_SHOW_RELEASE_NOTES": "false",
    "AIDER_DETECT_URLS": "false",
    "LITELLM_LOCAL_MODEL_COST_MAP": "True",
}


def _aider_install() -> str:
    """Return the command that pins Aider to :data:`AIDER_VERSION` and logs the version that will run.

    Harbor runs Aider's own ``install.sh``, which always takes the latest
    release; this pins uv and the package instead.

    Returns:
        The shell command.
    """
    return (
        f"{_AIDER_PATH}; {_version_check('aider', AIDER_VERSION)} || "
        "{ { command -v uv >/dev/null 2>&1 || "
        f"curl -LsSf https://astral.sh/uv/{AIDER_UV_VERSION}/install.sh | sh; }} && "
        f"uv tool install --force --python {AIDER_PYTHON} {AIDER_PACKAGE}=={AIDER_VERSION} >/dev/null; }} "
        "&& aider --version"
    )


def _aider_config(gateway: GatewayConfig, reasoning_effort: str | None) -> str:
    """Build Aider's YAML config for a non-interactive run against the gateway.

    The gateway URL lives in this file rather than on the command line, so a
    protected run's relay rewrite reaches it. The key stays in the environment.

    Args:
        gateway: Gateway URL and key.
        reasoning_effort: The user's effort for the model, or ``None`` for Aider's default.

    Returns:
        The YAML text. Each value is JSON, which YAML reads as the same scalar.
    """
    settings: dict[str, object] = {
        "openai-api-base": gateway.url,
        "read": ["AGENTS.md"],
        "yes-always": True,
        "auto-commits": False,
        "gitignore": False,
        "pretty": False,
        "fancy-input": False,
        "show-model-warnings": False,
        "analytics": False,
        "analytics-disable": True,
        "check-update": False,
        "show-release-notes": False,
        "detect-urls": False,
        "chat-history-file": ".app/aider/chat.history.md",
        "input-history-file": ".app/aider/input.history",
    }
    if reasoning_effort:
        settings["reasoning-effort"] = reasoning_effort
        # Aider drops the effort for a model its metadata does not list; a
        # gateway model id is never in that list.
        settings["check-model-accepts-settings"] = False
    return "".join(f"{key}: {json.dumps(value)}\n" for key, value in settings.items())


def _aider_launch(model: str, gateway: GatewayConfig, *, reasoning_effort: str | None = None) -> HarnessLaunch:
    """Build the launch for Aider (:data:`AIDER_PACKAGE`, pinned to :data:`AIDER_VERSION`).

    Aider has no JSON event stream, so the bridge keeps its whole stdout as
    the answer.

    Args:
        model: Target model id.
        gateway: Gateway URL and key.
        reasoning_effort: The user's effort for the model, or ``None`` for Aider's default.

    Returns:
        The launch.
    """
    return HarnessLaunch(
        instructions_file="AGENTS.md",
        install_command=_aider_install(),
        files={AIDER_CONFIG_FILE: _aider_config(gateway, reasoning_effort)},
        run_command=(
            f"{_AIDER_PATH}; {_SNAPSHOT_UNTRACKED_WORKSPACE}"
            f'OPENAI_API_KEY="$APP_API_KEY" aider --config {AIDER_CONFIG_FILE} '
            f'--model "{_LITELLM_PREFIX}$APP_MODEL" --message-file "${ENV_PROMPT_FILE}"'
        ),
        env=_base_env(model, gateway),
        parse_output=parse_plain_output,
    )


SPEC = register(
    HarnessSpec(
        id=AIDER_ID,
        launch=_aider_launch,
        binary="aider",
        version=AIDER_VERSION,
        output_format="plain",
        protected_env=_PROTECTED_ENV,
        position=50,
    )
)
