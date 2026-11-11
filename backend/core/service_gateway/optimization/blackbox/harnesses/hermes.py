"""Hermes Agent harness (``hermes-agent``).

Adapted from Harbor 0.25.0 (Apache-2.0), agents/installed/hermes: the
generated ``config.yaml`` in a private ``HERMES_HOME``, the ``custom:runtime``
OpenAI-compatible provider profile, and the ``--yolo chat -q ... -Q`` one-shot
run. Harbor installs the latest ``main`` through a piped install script;
Skynet installs the pinned PyPI wheel after a sha256 check instead, and points
the profile at the model gateway.
"""

from __future__ import annotations

import json
import shlex

from .....models.blackbox import BLACKBOX_HARNESS_HERMES
from ..harness_bridge import parse_plain_output
from . import HarnessSpec, register
from .base import _PROMPT_ARG, ENV_API_KEY, ENV_MODEL, GatewayConfig, HarnessLaunch, _base_env, _version_check

HERMES_PACKAGE = "hermes-agent"
# PyPI release of NousResearch/hermes-agent (MIT), checked on 2026-11-08.
HERMES_VERSION = "0.19.0"
# ``hermes --version`` prints ``Hermes Agent v0.19.0 (2026.7.20)``; the shared
# probe needs the token exactly as printed, so the pin carries the ``v``.
HERMES_VERSION_TOKEN = f"v{HERMES_VERSION}"
HERMES_WHEEL = f"hermes_agent-{HERMES_VERSION}-py3-none-any.whl"
HERMES_WHEEL_URL = (
    "https://files.pythonhosted.org/packages/e5/30/"
    f"c85be8290e9565dc3c7a9720e93f3e59e09b1b163487be4946c3aa848f80/{HERMES_WHEEL}"
)
# PyPI's sha256 digest for :data:`HERMES_WHEEL`.
HERMES_WHEEL_SHA256 = "bd0bac012aee38a60894781f4597dc29ee7bedb3448540249921f10d3bef327f"
# ``hermes-agent`` requires Python >= 3.11 and < 3.14.
HERMES_PYTHON = "3.12"
_UV_PIN = "uv==0.9.13"
# The gateway URL lives in these files rather than on the command line, so a
# protected run's relay rewrite of generated files reaches both.
HERMES_CONFIG_FILE = ".app/hermes/config.yaml"
HERMES_BASE_URL_FILE = ".app/hermes/base_url"
HERMES_PROVIDER = "custom:runtime"
# ``uv tool install`` puts its binaries here.
_HERMES_PATH = 'export PATH="$HOME/.local/bin:$PATH"'
# Downloads one file and keeps it only when its sha256 matches; ``-I`` keeps
# the interpreter from importing anything from the working directory.
_WHEEL_FETCH = (
    "import hashlib,sys,urllib.request; url,sha,dest=sys.argv[1:]; "
    "data=urllib.request.urlopen(url,timeout=120).read(); "
    "hashlib.sha256(data).hexdigest()==sha or sys.exit('hermes wheel: sha256 mismatch'); "
    "open(dest,'wb').write(data)"
)
# ``hermes chat`` always checks PyPI for a newer release and has no switch to
# stop it. It skips the check while ``$HERMES_HOME/.update_check`` is younger
# than six hours and names the running version, so each run seeds that cache.
_UPDATE_CHECK_CACHE = '{"ts": %s, "rev": null, "ver": "' + HERMES_VERSION + '", "behind": 0}'
# Hermes's ``agent.reasoning_effort`` levels; any other effort gets no setting.
_HERMES_EFFORTS = frozenset({"none", "minimal", "low", "medium", "high", "xhigh", "max", "ultra"})
# Hermes reads these keys for its native providers and Langfuse tracing. A
# protected run blanks them, so an inherited key never gives Hermes a route
# around the gateway. Hermes documents no telemetry switch.
_PROTECTED_ENV = {
    "ANTHROPIC_API_KEY": "",
    "ANTHROPIC_TOKEN": "",
    "OPENROUTER_API_KEY": "",
    "GLM_API_KEY": "",
    "ZAI_API_KEY": "",
    "Z_AI_API_KEY": "",
    "KIMI_API_KEY": "",
    "KIMI_CN_API_KEY": "",
    "MINIMAX_API_KEY": "",
    "MINIMAX_CN_API_KEY": "",
    "GEMINI_API_KEY": "",
    "GOOGLE_API_KEY": "",
    "XAI_API_KEY": "",
    "DEEPSEEK_API_KEY": "",
    "MISTRAL_API_KEY": "",
    "NVIDIA_API_KEY": "",
    "HF_TOKEN": "",
    "DASHSCOPE_API_KEY": "",
    "FIREWORKS_API_KEY": "",
    "LANGFUSE_PUBLIC_KEY": "",
    "LANGFUSE_SECRET_KEY": "",
    "LANGFUSE_BASE_URL": "",
    "DO_NOT_TRACK": "1",
}


def _hermes_install() -> str:
    """Return the command that installs the checksum-verified :data:`HERMES_WHEEL` and logs its version.

    Returns:
        The shell command.
    """
    fetch = (
        f"python3 -I -c {shlex.quote(_WHEEL_FETCH)} {HERMES_WHEEL_URL} {HERMES_WHEEL_SHA256} "
        f'"$_hermes_wheel/{HERMES_WHEEL}"'
    )
    return (
        f"{_HERMES_PATH}; {_version_check('hermes', HERMES_VERSION_TOKEN)} || "
        "{ { command -v uv >/dev/null 2>&1 || "
        f"python3 -m pip install --disable-pip-version-check --no-deps --user {_UV_PIN}; }} && "
        f'_hermes_wheel="$(mktemp -d)" && {fetch} && '
        f'uv tool install --force --python {HERMES_PYTHON} "$_hermes_wheel/{HERMES_WHEEL}" >/dev/null; }} '
        "&& hermes --version"
    )


def _hermes_config(model: str, gateway: GatewayConfig, reasoning_effort: str | None) -> str:
    """Build Hermes's ``config.yaml`` for a one-shot run against the gateway.

    The profile names the key's environment variable, so the token never lands
    in the file.

    Args:
        model: Target model id.
        gateway: Gateway URL and key.
        reasoning_effort: The user's effort for the model, or ``None`` for Hermes's default.

    Returns:
        The config text. JSON is valid YAML.
    """
    agent: dict[str, object] = {}
    if reasoning_effort in _HERMES_EFFORTS:
        agent["reasoning_effort"] = reasoning_effort
    config: dict[str, object] = {
        "model": model,
        "provider": HERMES_PROVIDER,
        "toolsets": ["hermes-cli"],
        "custom_providers": [
            {
                "name": "runtime",
                "model": model,
                "base_url": gateway.url,
                "key_env": "OPENAI_API_KEY",
                "api_mode": "chat_completions",
            }
        ],
        "fallback_providers": [],
        "memory": {"memory_enabled": False, "user_profile_enabled": False},
        "terminal": {"backend": "local", "timeout": 180},
        "delegation": {"max_iterations": 50},
        "checkpoints": {"enabled": False},
        **({"agent": agent} if agent else {}),
    }
    return json.dumps(config, indent=2) + "\n"


def _hermes_launch(model: str, gateway: GatewayConfig, *, reasoning_effort: str | None = None) -> HarnessLaunch:
    """Build the launch for Hermes Agent (:data:`HERMES_PACKAGE`, pinned to :data:`HERMES_VERSION`).

    Each run gets a fresh ``HERMES_HOME`` outside the workspace, so Hermes's
    sessions and logs never join the files the run hands back. ``OPENAI_BASE_URL``
    also names the gateway, so a side task that Hermes routes to its OpenAI
    provider still reaches the gateway. ``-Q`` prints only the final answer.

    Args:
        model: Target model id.
        gateway: Gateway URL and key.
        reasoning_effort: The user's effort for the model, or ``None`` for Hermes's default.

    Returns:
        The launch.
    """
    run_command = (
        f'{_HERMES_PATH}; export HERMES_HOME="$(mktemp -d)" && '
        f'cp {HERMES_CONFIG_FILE} "$HERMES_HOME/config.yaml" && '
        f'printf {shlex.quote(_UPDATE_CHECK_CACHE)} "$(date +%s)" > "$HERMES_HOME/.update_check" && '
        f'OPENAI_API_KEY="${ENV_API_KEY}" OPENAI_BASE_URL="$(cat {HERMES_BASE_URL_FILE})" '
        f'hermes --yolo chat -q {_PROMPT_ARG} -Q --model "${ENV_MODEL}" --provider {HERMES_PROVIDER} < /dev/null'
    )
    return HarnessLaunch(
        instructions_file="AGENTS.md",
        install_command=_hermes_install(),
        files={
            HERMES_CONFIG_FILE: _hermes_config(model, gateway, reasoning_effort),
            HERMES_BASE_URL_FILE: gateway.url,
        },
        run_command=run_command,
        env=_base_env(model, gateway),
        parse_output=parse_plain_output,
    )


SPEC = register(
    HarnessSpec(
        id=BLACKBOX_HARNESS_HERMES,
        launch=_hermes_launch,
        binary="hermes",
        version=HERMES_VERSION_TOKEN,
        output_format="plain",
        protected_env=_PROTECTED_ENV,
        position=68,
    )
)
