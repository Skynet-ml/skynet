"""OpenHands harness (``openhands``).

Adapted from Harbor 0.25.0 (Apache-2.0), agents/installed/openhands: the uv
virtualenv install of ``openhands-ai`` with ``SKIP_VSCODE_BUILD``, the local
runtime settings (``RUNTIME=local``, ``SKIP_DEPENDENCY_CHECK``, no browser)
and the headless ``python -m openhands.core.main`` one-shot with
``SANDBOX_VOLUMES`` on the working directory and logs on stdout. Harbor passes
the task inline with ``--task`` and turns prompt extensions off; Skynet passes
the prompt file with ``-f``, keeps prompt extensions on so the candidate's
``AGENTS.md`` loads, and points LiteLLM at the model gateway.
"""

from __future__ import annotations

from .....models.blackbox import BLACKBOX_HARNESS_OPENHANDS
from ..harness_bridge import parse_plain_output
from . import HarnessSpec, register
from .base import ENV_API_KEY, ENV_MODEL, ENV_PROMPT_FILE, GatewayConfig, HarnessLaunch, _base_env, _version_check

# PyPI ``openhands-ai`` 1.6.0 (MIT), 2026-03-30. It is the last release with
# the headless ``openhands.core.main`` entry point; 1.7.0 removed it.
OPENHANDS_VERSION = "1.6.0"
OPENHANDS_PACKAGE = "openhands-ai"
OPENHANDS_PYTHON = "3.12"
OPENHANDS_UV_VERSION = "0.13.7"
OPENHANDS_VENV = '"$HOME/.local/share/openhands-venv"'
# The gateway URL lives in this file rather than on the command line, so a
# protected run's relay rewrite of generated files reaches it.
OPENHANDS_BASE_URL_FILE = ".app/openhands/base_url"
# OpenHands runs on LiteLLM, which routes an ``openai/`` model to the
# OpenAI-style base URL; the gateway receives the id after the prefix.
_LITELLM_PREFIX = "openai/"
_LOCAL_BIN = '"$HOME/.local/bin"'
# The wheel ships no console script, so the install writes this wrapper; the
# shared pin check then runs ``openhands --version`` like any other harness.
_WRAPPER = '#!/bin/sh\\nexec "%s/bin/python" -m openhands.core.main "$@"\\n'
# LiteLLM's ``reasoning_effort`` takes only low, medium and high here; other
# Skynet effort names map to the closest one, and unknown names set nothing.
_REASONING_LEVELS = {"minimal": "low", "low": "low", "medium": "medium", "high": "high", "xhigh": "high", "max": "high"}
# LiteLLM reads these keys for its other providers. A protected run blanks
# them, so an inherited key never gives OpenHands a route around the gateway.
_PROTECTED_ENV = {
    "OPENAI_API_KEY": "",
    "ANTHROPIC_API_KEY": "",
    "GOOGLE_API_KEY": "",
    "GEMINI_API_KEY": "",
    "OPENROUTER_API_KEY": "",
    "AZURE_API_KEY": "",
    "AZURE_OPENAI_API_KEY": "",
    "GROQ_API_KEY": "",
    "XAI_API_KEY": "",
    "MISTRAL_API_KEY": "",
    "DEEPSEEK_API_KEY": "",
    # The Tavily search tool calls an outside service with this key.
    "SEARCH_API_KEY": "",
    # A protected sandbox has no route to the internet: LiteLLM keeps its
    # bundled cost map instead of fetching one.
    "LITELLM_LOCAL_MODEL_COST_MAP": "True",
    "DO_NOT_TRACK": "1",
}


def _openhands_install() -> str:
    """Return a command that installs the pinned OpenHands release unless it is already on PATH.

    Harbor installs uv and Python, then the package, into ``/opt``; this pins
    uv and the package and keeps everything under ``$HOME``.

    Returns:
        The shell command. It creates a uv virtualenv, installs
        ``openhands-ai`` into it, writes the ``openhands`` wrapper into
        ``$HOME/.local/bin`` and logs the version that will run. It exits
        non-zero when a step fails.
    """
    # The bootstrap runs this inside its own ``set -eu`` script, where a
    # failure inside an ``||`` list does not stop the script, so the step
    # exits with the failed status itself.
    return (
        f'export PATH={_LOCAL_BIN}:"$PATH"; '
        f"{_version_check('openhands', OPENHANDS_VERSION)} || "
        "{ { command -v uv >/dev/null 2>&1 || "
        f"curl -LsSf https://astral.sh/uv/{OPENHANDS_UV_VERSION}/install.sh | sh; }} && "
        f"uv venv --clear --python {OPENHANDS_PYTHON} {OPENHANDS_VENV} >/dev/null && "
        f"SKIP_VSCODE_BUILD=true uv pip install --python {OPENHANDS_VENV}/bin/python "
        f"{OPENHANDS_PACKAGE}=={OPENHANDS_VERSION} >/dev/null && "
        f"mkdir -p {_LOCAL_BIN} && "
        f"printf '{_WRAPPER}' {OPENHANDS_VENV} > {_LOCAL_BIN}/openhands && "
        f'chmod 755 {_LOCAL_BIN}/openhands || exit "$?"; }}; '
        "openhands --version"
    )


def _openhands_reasoning(reasoning_effort: str | None) -> dict[str, str]:
    """Return the ``LLM_REASONING_EFFORT`` setting for the user's effort, or nothing for the model default.

    Args:
        reasoning_effort: The user's effort for the model, or ``None``.

    Returns:
        The env entry, or an empty dict.
    """
    level = _REASONING_LEVELS.get(reasoning_effort or "")
    return {"LLM_REASONING_EFFORT": level} if level else {}


def _openhands_launch(model: str, gateway: GatewayConfig, *, reasoning_effort: str | None = None) -> HarnessLaunch:
    """Build the launch for OpenHands (``openhands``, pinned to :data:`OPENHANDS_VERSION`).

    Each run gets a fresh OpenHands root outside the workspace for the file
    store, the cache and an empty config file, so session files never land in
    the files the run hands back and a ``config.toml`` in the workspace cannot
    change the model route. An EXIT trap removes the root when the run's shell
    exits and keeps the agent's exit status. OpenHands writes its log, with the
    agent's messages, to stderr, so the run merges it into stdout for the
    bridge, as Harbor does.

    Args:
        model: Target model id.
        gateway: Gateway URL and key.
        reasoning_effort: The user's effort for the model, or ``None`` for the model default.

    Returns:
        The launch.
    """
    run_command = (
        f'export PATH={_LOCAL_BIN}:"$PATH" && '
        'export OPENHANDS_ROOT="$(mktemp -d)" && '
        "trap 'rm -rf \"$OPENHANDS_ROOT\"' EXIT && "
        f'LLM_BASE_URL="$(cat {OPENHANDS_BASE_URL_FILE})" LLM_API_KEY="${ENV_API_KEY}" '
        f'LLM_MODEL="{_LITELLM_PREFIX}${ENV_MODEL}" '
        'FILE_STORE_PATH="$OPENHANDS_ROOT/store" CACHE_DIR="$OPENHANDS_ROOT/cache" '
        'SANDBOX_VOLUMES="$PWD:/workspace:rw" USER="$(id -un)" '
        'openhands --config-file "$OPENHANDS_ROOT/config.toml" '
        f'-f "${ENV_PROMPT_FILE}" 2>&1 < /dev/null'
    )
    env = _base_env(model, gateway)
    env.update(
        {
            "RUNTIME": "local",
            # Harbor skips the local runtime's startup check for tmux and
            # libtmux the same way; the sandbox image still needs tmux.
            "SKIP_DEPENDENCY_CHECK": "1",
            "RUN_AS_OPENHANDS": "false",
            "SU_TO_USER": "false",
            "FILE_STORE": "local",
            "LOG_TO_FILE": "false",
            "AGENT_ENABLE_BROWSING": "false",
            "ENABLE_BROWSER": "false",
            "SANDBOX_ENABLE_AUTO_LINT": "true",
            # Harbor turns prompt extensions off. Skynet keeps them on: they
            # load the workspace ``AGENTS.md``, which holds the candidate.
            "AGENT_ENABLE_PROMPT_EXTENSIONS": "true",
            **_openhands_reasoning(reasoning_effort),
        }
    )
    return HarnessLaunch(
        instructions_file="AGENTS.md",
        install_command=_openhands_install(),
        files={OPENHANDS_BASE_URL_FILE: gateway.url},
        run_command=run_command,
        env=env,
        parse_output=parse_plain_output,
    )


SPEC = register(
    HarnessSpec(
        id=BLACKBOX_HARNESS_OPENHANDS,
        launch=_openhands_launch,
        binary="openhands",
        version=OPENHANDS_VERSION,
        output_format="plain",
        protected_env=_PROTECTED_ENV,
        position=54,
    )
)
