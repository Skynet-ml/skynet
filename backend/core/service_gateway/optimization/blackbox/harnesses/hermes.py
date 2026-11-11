"""Hermes Agent harness (``hermes-agent``).

Adapted from Harbor 0.25.0 (Apache-2.0), agents/installed/hermes: the
generated ``config.yaml`` in a private ``HERMES_HOME``, the ``custom:runtime``
OpenAI-compatible provider profile, and the ``--yolo chat -q ... -Q`` one-shot
run. Harbor pipes the upstream install script into a shell and takes the
latest ``main``; Skynet installs the pinned PyPI wheel after a sha256 check,
with a hash-checked uv, and points the profile at the model gateway.
"""

from __future__ import annotations

import json
import shlex

from .....exceptions import ServiceError
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
UV_VERSION = "0.9.13"
# PyPI's sha256 digests for the x86_64 and aarch64 Linux wheels of uv
# :data:`UV_VERSION`; pip installs only a wheel that matches one of them.
UV_WHEEL_SHA256 = (
    "6627d0abbaf58f9ff6e07e3f8522d65421230969aa2d7b10421339f0cb30dec4",
    "d50cea327b994786866b2d12c073097b9c8d883d42f0c0408b2d968492f571a4",
    "6a641ed7bcc8d317d22a7cb1ad0dfa41078c8e392f6f9248b11451abff0ccf50",
    "6cca7671efacf6e2950eb86273ecce4a9a3f8bfa6ac04e8a17be9499bb3bb882",
)
_UV_REQUIREMENT = f"uv=={UV_VERSION} " + " ".join(f"--hash=sha256:{digest}" for digest in UV_WHEEL_SHA256)
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
# The Skynet levels that ``hermes_constants.parse_reasoning_effort`` accepts in
# 0.19.0. Hermes also accepts ``ultra``, which is not a Skynet level. Hermes
# ignores an unknown value without a warning, so submit and launch refuse it.
HERMES_EFFORTS = ("none", "minimal", "low", "medium", "high", "xhigh", "max")
# Hermes 0.19.0 reads these keys for its native providers, its auxiliary
# fallback chain and Langfuse tracing. A protected run blanks them, so an
# inherited key never gives Hermes a route around the gateway. The run sets
# ``OPENAI_API_KEY`` to the gateway token on the command itself. Hermes sends
# no telemetry of its own; DO_NOT_TRACK covers the tools it starts.
_PROTECTED_ENV = {
    "OPENAI_API_KEY": "",
    "ANTHROPIC_API_KEY": "",
    "ANTHROPIC_TOKEN": "",
    "CLAUDE_CODE_OAUTH_TOKEN": "",
    "OPENROUTER_API_KEY": "",
    "GLM_API_KEY": "",
    "ZAI_API_KEY": "",
    "Z_AI_API_KEY": "",
    "KIMI_API_KEY": "",
    "KIMI_CN_API_KEY": "",
    "KIMI_CODING_API_KEY": "",
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
    "ALIBABA_CODING_PLAN_API_KEY": "",
    "ARCEEAI_API_KEY": "",
    "AZURE_FOUNDRY_API_KEY": "",
    "GMI_API_KEY": "",
    "KILOCODE_API_KEY": "",
    "LM_API_KEY": "",
    "OLLAMA_API_KEY": "",
    "OPENCODE_GO_API_KEY": "",
    "OPENCODE_ZEN_API_KEY": "",
    "STEPFUN_API_KEY": "",
    "TOKENHUB_API_KEY": "",
    "UPSTAGE_API_KEY": "",
    "XIAOMI_API_KEY": "",
    "NOUS_API_KEY": "",
    "DEEPINFRA_API_KEY": "",
    "NOVITA_API_KEY": "",
    "QWEN_API_KEY": "",
    # Not read by 0.19.0; blanked so a later Hermes cannot pick it up.
    "AI_GATEWAY_API_KEY": "",
    # Speech tools call Groq and OpenAI audio with their own keys.
    "GROQ_API_KEY": "",
    "VOICE_TOOLS_OPENAI_KEY": "",
    # Side tasks (vision, web extract, approval) can take their own endpoint key.
    "AUXILIARY_VISION_API_KEY": "",
    "AUXILIARY_WEB_EXTRACT_API_KEY": "",
    "AUXILIARY_APPROVAL_API_KEY": "",
    # The Copilot provider takes a GitHub token as its model key.
    "COPILOT_GITHUB_TOKEN": "",
    "GH_TOKEN": "",
    "GITHUB_TOKEN": "",
    "LANGFUSE_PUBLIC_KEY": "",
    "LANGFUSE_SECRET_KEY": "",
    "LANGFUSE_BASE_URL": "",
    "DO_NOT_TRACK": "1",
}


def _hermes_install() -> str:
    """Return the command that installs the checksum-verified :data:`HERMES_WHEEL` and logs its version.

    Returns:
        The shell command. When uv is missing, pip installs the pinned uv into
        the download folder and checks its digest. The command removes that
        folder on success and on failure, and exits non-zero when a step fails.
    """
    fetch = f'python3 -I -c {shlex.quote(_WHEEL_FETCH)} {HERMES_WHEEL_URL} {HERMES_WHEEL_SHA256} "$d/{HERMES_WHEEL}"'
    uv = (
        '{ u="$(command -v uv)" || { '
        f'printf "%s\\n" {shlex.quote(_UV_REQUIREMENT)} > "$d/uv.txt" && '
        "python3 -m pip install -q --disable-pip-version-check --no-deps --only-binary :all: "
        '--require-hashes --target "$d/uv" -r "$d/uv.txt" && u="$d/uv/bin/uv"; }; }'
    )
    # The bootstrap runs this inside its own ``set -eu`` script, so the step
    # records its status with ``|| r=$?`` (exempt from ``set -e``) and removes
    # the download folder before it fails, rather than with an EXIT trap that
    # would replace the caller's.
    return (
        f"{_HERMES_PATH}; {_version_check('hermes', HERMES_VERSION_TOKEN)} || {{ "
        'd="$(mktemp -d)"; r=0; '
        f"{uv} && {fetch} && "
        f'"$u" tool install --force --python {HERMES_PYTHON} "$d/{HERMES_WHEEL}" >/dev/null || r=$?; '
        'rm -rf "$d"; [ "$r" -eq 0 ] || exit "$r"; } && hermes --version'
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

    Raises:
        ServiceError: When the effort is not one of :data:`HERMES_EFFORTS`.
    """
    agent: dict[str, object] = {}
    if reasoning_effort:
        if reasoning_effort not in HERMES_EFFORTS:
            raise ServiceError(
                f"Hermes does not support the reasoning effort '{reasoning_effort}'. "
                f"Use one of: {', '.join(HERMES_EFFORTS)}."
            )
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
    sessions and logs never join the files the run hands back. An EXIT trap
    removes it when the run's shell exits and keeps Hermes's exit status.
    Hermes has no prompt-file option, so the prompt is one argument that the
    shell reads from the prompt file and never parses. ``OPENAI_BASE_URL``
    also names the gateway, so a side task that Hermes routes to its OpenAI
    provider still reaches the gateway. ``-Q`` prints only the final answer.

    Args:
        model: Target model id.
        gateway: Gateway URL and key.
        reasoning_effort: The user's effort for the model, or ``None`` for Hermes's default.

    Returns:
        The launch.

    Raises:
        ServiceError: When the effort is not one of :data:`HERMES_EFFORTS`.
    """
    run_command = (
        f'{_HERMES_PATH}; export HERMES_HOME="$(mktemp -d)" && '
        "trap 'rm -rf \"$HERMES_HOME\"' EXIT && "
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
        position=65,
        supported_efforts=HERMES_EFFORTS,
    )
)
