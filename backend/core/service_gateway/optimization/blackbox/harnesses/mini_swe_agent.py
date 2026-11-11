"""mini-swe-agent harness (``mini-swe-agent``).

Adapted from Harbor 0.25.0 (Apache-2.0), agents/installed/mini_swe_agent.py:
the install through uv, the ``--yolo --exit-immediately`` one-shot run on the
built-in ``mini`` config, the LiteLLM ``openai/`` route, the one-hour LiteLLM
timeout and ``MSWEA_CONFIGURED``. Harbor installs the latest uv, gives mini a
direct provider key and writes the trajectory into the agent logs; Skynet pins
uv and the package, points LiteLLM at the model gateway through a config file,
and keeps mini's global config and trajectory in a per-run folder it removes.
"""

from __future__ import annotations

import json
import shlex

from .....models.blackbox import BLACKBOX_HARNESS_MINI_SWE_AGENT
from ..harness_bridge import parse_plain_output
from . import HarnessSpec, register
from .base import ENV_API_KEY, ENV_MODEL, ENV_PROMPT_FILE, GatewayConfig, HarnessLaunch, _base_env, _version_check

MINI_SWE_AGENT_PACKAGE = "mini-swe-agent"
# PyPI release 2.4.6 (MIT), 2026-07-23.
MINI_SWE_AGENT_VERSION = "2.4.6"
# The same uv and Python pins as the Aider harness, so one image layer serves both.
MINI_SWE_AGENT_UV_VERSION = "0.13.7"
MINI_SWE_AGENT_PYTHON = "3.12"
MINI_SWE_AGENT_CONFIG_FILE = ".app/mini_swe_agent/config.yaml"
# mini runs on LiteLLM, which routes an ``openai/`` model to the ``api_base``
# in the config file; the gateway receives the id after the prefix.
_LITELLM_PREFIX = "openai/"
# ``uv tool install`` and the uv installer both put their binaries here.
_MINI_PATH = 'export PATH="$HOME/.local/bin:$PATH"'
# mini has no ``--version`` option, so the probe asks the tool's own Python
# for the installed distribution. It reads package metadata only: importing
# ``minisweagent`` would create its global config folder. ``--version`` lands
# in ``sys.argv`` and is ignored.
_PROBE = f'"$(uv tool dir 2>/dev/null)/{MINI_SWE_AGENT_PACKAGE}/bin/python" -c ' + shlex.quote(
    f'import importlib.metadata as m; print(m.version("{MINI_SWE_AGENT_PACKAGE}"))'
)
# mini only takes the task as an argument. The candidate instructions go first,
# because mini does not read AGENTS.md itself. A named-parts version may have
# no AGENTS.md. ``$(...)`` output is never parsed again by the shell.
_TASK_ARG = f'"$(if [ -f AGENTS.md ]; then cat AGENTS.md; printf \'\\n\\n\'; fi; cat "${ENV_PROMPT_FILE}")"'
# mini and LiteLLM read these keys for their other routes. A protected run
# blanks them, so an inherited key never gives mini a route around the gateway.
_PROTECTED_ENV = {
    "ANTHROPIC_API_KEY": "",
    "GOOGLE_API_KEY": "",
    "GEMINI_API_KEY": "",
    "OPENROUTER_API_KEY": "",
    "PORTKEY_API_KEY": "",
    "PORTKEY_VIRTUAL_KEY": "",
    "REQUESTY_API_KEY": "",
    "GROQ_API_KEY": "",
    "XAI_API_KEY": "",
    "MISTRAL_API_KEY": "",
    "DEEPSEEK_API_KEY": "",
    "AZURE_API_KEY": "",
    "AZURE_OPENAI_API_KEY": "",
    # A protected sandbox has no route to the internet: LiteLLM keeps its
    # bundled cost map instead of fetching one.
    "LITELLM_LOCAL_MODEL_COST_MAP": "True",
}


def _mini_install() -> str:
    """Return the command that pins mini-swe-agent to :data:`MINI_SWE_AGENT_VERSION` and logs the version that will run.

    Harbor installs the latest uv; this pins uv and the package.

    Returns:
        The shell command.
    """
    check = _version_check(_PROBE, MINI_SWE_AGENT_VERSION)
    return (
        f"{_MINI_PATH}; {check} || "
        "{ { command -v uv >/dev/null 2>&1 || "
        f"curl -LsSf https://astral.sh/uv/{MINI_SWE_AGENT_UV_VERSION}/install.sh | sh; }} && "
        f"uv tool install --force --python {MINI_SWE_AGENT_PYTHON} "
        f"{MINI_SWE_AGENT_PACKAGE}=={MINI_SWE_AGENT_VERSION} >/dev/null; }} "
        f"&& {_PROBE}"
    )


def _mini_config(gateway: GatewayConfig, reasoning_effort: str | None) -> str:
    """Build the YAML config mini merges over its built-in ``mini`` config.

    The gateway URL lives in this file rather than on the command line, so a
    protected run's relay rewrite reaches it. The key stays in the environment.

    Args:
        gateway: Gateway URL and key.
        reasoning_effort: The user's effort for the model, or ``None`` for the model's default.

    Returns:
        The YAML text. JSON is valid YAML, so ``json.dumps`` writes it.
    """
    model_kwargs: dict[str, object] = {
        "api_base": gateway.url,
        # LiteLLM's default 600 s timeout cuts off long reasoning calls; Harbor uses one hour.
        "timeout": 3600,
    }
    if reasoning_effort:
        model_kwargs["reasoning_effort"] = reasoning_effort
    config = {
        # Skynet's run budget bounds the spend. LiteLLM has no prices for
        # gateway model ids, and mini's default $3 limit would stop a run early.
        "agent": {"cost_limit": 0},
        "model": {"model_class": "litellm", "cost_tracking": "ignore_errors", "model_kwargs": model_kwargs},
    }
    return json.dumps(config, indent=2) + "\n"


def _mini_launch(model: str, gateway: GatewayConfig, *, reasoning_effort: str | None = None) -> HarnessLaunch:
    """Build the launch for mini-swe-agent (:data:`MINI_SWE_AGENT_PACKAGE`, pinned to :data:`MINI_SWE_AGENT_VERSION`).

    Each run points ``MSWEA_GLOBAL_CONFIG_DIR`` at a fresh folder outside the
    workspace, so mini loads no inherited ``.env`` and its trajectory file never
    lands in the files the run hands back. An EXIT trap removes the folder and
    keeps mini's exit status. mini has no JSON event stream, so the bridge keeps
    its whole stdout as the answer.

    Args:
        model: Target model id.
        gateway: Gateway URL and key.
        reasoning_effort: The user's effort for the model, or ``None`` for the model's default.

    Returns:
        The launch.
    """
    run_command = (
        f"{_MINI_PATH} && "
        'export MSWEA_GLOBAL_CONFIG_DIR="$(mktemp -d)" && '
        "trap 'rm -rf \"$MSWEA_GLOBAL_CONFIG_DIR\"' EXIT && "
        f'OPENAI_API_KEY="${ENV_API_KEY}" '
        f'mini-swe-agent --yolo --exit-immediately --model "{_LITELLM_PREFIX}${ENV_MODEL}" '
        f"-c mini -c {MINI_SWE_AGENT_CONFIG_FILE} --task {_TASK_ARG} < /dev/null"
    )
    env = _base_env(model, gateway)
    env.update(
        {
            # Skips mini's interactive first-run setup, which would wait on stdin.
            "MSWEA_CONFIGURED": "true",
            "MSWEA_SILENT_STARTUP": "1",
            "MSWEA_COST_TRACKING": "ignore_errors",
        }
    )
    return HarnessLaunch(
        instructions_file="AGENTS.md",
        install_command=_mini_install(),
        files={MINI_SWE_AGENT_CONFIG_FILE: _mini_config(gateway, reasoning_effort)},
        run_command=run_command,
        env=env,
        parse_output=parse_plain_output,
    )


SPEC = register(
    HarnessSpec(
        id=BLACKBOX_HARNESS_MINI_SWE_AGENT,
        launch=_mini_launch,
        binary=_PROBE,
        version=MINI_SWE_AGENT_VERSION,
        output_format="plain",
        protected_env=_PROTECTED_ENV,
        position=52,
    )
)
