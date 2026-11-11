"""Qwen Code CLI harness (``qwen``).

Adapted from Harbor 0.25.0 (Apache-2.0), agents/installed/qwen_code: the npm
install of ``@qwen-code/qwen-code`` and the ``--yolo --auth-type openai``
one-shot run with ``--prompt``. Harbor installs through nvm, passes a direct
provider key with ``--openai-api-key`` and records the chat under
``~/.qwen``; Skynet pins one release, gives Qwen Code the gateway token only
through ``OPENAI_API_KEY``, points ``OPENAI_BASE_URL`` at the model gateway,
and keeps Qwen Code's home in a per-run temp folder.
"""

from __future__ import annotations

from .....models.blackbox import BLACKBOX_HARNESS_QWEN_CODE
from ..harness_bridge import parse_plain_output
from . import HarnessSpec, register
from .base import _PROMPT_ARG, ENV_API_KEY, ENV_MODEL, GatewayConfig, HarnessLaunch, _base_env, _npm_install

# npm registry ``dist-tags.latest`` for ``@qwen-code/qwen-code`` on 2026-11-10 (Apache-2.0).
QWEN_CODE_VERSION = "0.27.2"
QWEN_CODE_PACKAGE = "@qwen-code/qwen-code"
# The gateway URL lives in this file rather than on the command line, so a
# protected run's relay rewrite of generated files reaches it.
QWEN_CODE_BASE_URL_FILE = ".app/qwen/base_url"
# Qwen Code 0.27.2 loads a key from a workspace ``.env`` or ``.qwen/.env`` file
# when the process env holds no value or an empty one, so an empty value would
# let the candidate repo refill it. A non-empty value that no provider accepts
# keeps the key out of reach.
_PROTECTED_KEY_VALUE = "skynet-protected-unset"
# Qwen Code reads these keys for its other auth types and providers. A
# protected run overrides them, so an inherited or workspace key never gives
# Qwen Code a route around the gateway.
_PROTECTED_ENV = {
    "QWEN_API_KEY": _PROTECTED_KEY_VALUE,
    # Also the fallback key of the web search providers.
    "DASHSCOPE_API_KEY": _PROTECTED_KEY_VALUE,
    "ANTHROPIC_API_KEY": _PROTECTED_KEY_VALUE,
    "ANTHROPIC_AUTH_TOKEN": _PROTECTED_KEY_VALUE,
    "GEMINI_API_KEY": _PROTECTED_KEY_VALUE,
    "GOOGLE_API_KEY": _PROTECTED_KEY_VALUE,
    "OPENROUTER_API_KEY": _PROTECTED_KEY_VALUE,
    # Any non-empty QWEN_OAUTH selects the OAuth sign-in in
    # ``getAuthTypeFromEnv``, so this key stays empty. A workspace refill has
    # no effect on the run: ``--auth-type openai`` wins over the env.
    "QWEN_OAUTH": "",
    # A non-empty ENABLE_WEB_SEARCH wins over the workspace
    # ``tools.webSearch.enabled`` setting.
    "ENABLE_WEB_SEARCH": "false",
}


def _qwen_code_launch(model: str, gateway: GatewayConfig, *, reasoning_effort: str | None = None) -> HarnessLaunch:
    """Build the launch for Qwen Code (``qwen``, pinned to :data:`QWEN_CODE_VERSION`).

    Each run gets a fresh ``QWEN_HOME`` outside the workspace, so the settings,
    chat history and installation id never land in the files the run hands
    back. An EXIT trap removes it when the run's shell exits and keeps Qwen
    Code's exit status. The plain text that ``--prompt`` mode prints is the
    answer.

    Args:
        model: Target model id.
        gateway: Gateway URL and key.
        reasoning_effort: Accepted for the shared launch signature and not
            used: Qwen Code 0.27.2 has no flag or env variable for effort, only
            a per-model settings entry.

    Returns:
        The launch.
    """
    del reasoning_effort
    # The token goes in OPENAI_API_KEY, never ``--openai-api-key``, so it stays
    # out of the process arguments. The inline OPENAI_BASE_URL replaces any
    # inherited one, so model calls reach only the gateway or its relay.
    # Workspace settings can turn on web tools and MCP servers, which call
    # outside the gateway. ``--exclude-tools`` adds to the deny list after the
    # settings, and the tool scheduler denies listed tools also under
    # ``--yolo``. ``--allowed-mcp-server-names`` replaces the settings allow
    # list with a name no server has, and ``--disable-mcp-server '*'`` marks
    # every server disabled, so discovery starts none.
    run_command = (
        'export QWEN_HOME="$(mktemp -d)" && '
        "trap 'rm -rf \"$QWEN_HOME\"' EXIT && "
        f'OPENAI_BASE_URL="$(cat {QWEN_CODE_BASE_URL_FILE})" '
        f'OPENAI_API_KEY="${ENV_API_KEY}" OPENAI_MODEL="${ENV_MODEL}" '
        f'qwen --yolo --auth-type openai --model "${ENV_MODEL}" '
        "--exclude-tools web_fetch,web_search "
        "--allowed-mcp-server-names skynet-no-mcp --disable-mcp-server '*' "
        f"--prompt {_PROMPT_ARG} < /dev/null"
    )
    env = _base_env(model, gateway)
    env.update(
        {
            "QWEN_TELEMETRY_ENABLED": "false",
            "QWEN_USAGE_STATISTICS_ENABLED": "false",
            # Skips the startup npm update check and the auto-install it can start.
            "QWEN_CODE_SKIP_UPDATE_CHECK_ONCE": "true",
            # Stops the models.dev catalog download, a network call outside the gateway.
            "QWEN_CODE_MODELS_DEV": "off",
        }
    )
    return HarnessLaunch(
        instructions_file="AGENTS.md",
        install_command=_npm_install("qwen", QWEN_CODE_PACKAGE, QWEN_CODE_VERSION),
        files={QWEN_CODE_BASE_URL_FILE: gateway.url},
        run_command=run_command,
        env=env,
        parse_output=parse_plain_output,
    )


SPEC = register(
    HarnessSpec(
        id=BLACKBOX_HARNESS_QWEN_CODE,
        launch=_qwen_code_launch,
        binary="qwen",
        version=QWEN_CODE_VERSION,
        output_format="plain",
        protected_env=_PROTECTED_ENV,
        position=58,
    )
)
