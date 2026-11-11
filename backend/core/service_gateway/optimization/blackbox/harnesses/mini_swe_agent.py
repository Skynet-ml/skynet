"""mini-swe-agent harness (``mini-swe-agent``).

Adapted from Harbor 0.25.0 (Apache-2.0), agents/installed/mini_swe_agent.py:
the install through uv, the ``--yolo --exit-immediately`` one-shot run on the
built-in ``mini`` config, the LiteLLM ``openai/`` route, the one-hour LiteLLM
timeout and ``MSWEA_CONFIGURED``. Harbor installs the latest uv, gives mini a
direct provider key and writes the trajectory into the agent logs; Skynet pins
uv, the package, LiteLLM and a resolution date, points LiteLLM at the model
gateway through a config file, passes the task through a config file, and keeps
mini's global config and trajectory in a per-run folder it removes. mini's
transcript goes to stderr; stdout carries only the final answer.
"""

from __future__ import annotations

import json
import shlex

from .....exceptions import ServiceError
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
# mini only bounds LiteLLM from below. The backend locks and runs 1.103.1,
# which is inside mini's range and is not one of the compromised releases
# (1.82.7, 1.82.8) that mini excludes.
MINI_SWE_AGENT_LITELLM_VERSION = "1.103.1"
# uv resolves every other dependency as of this date, so a later release of
# one of them never reaches a run. It is after the LiteLLM pin's release.
MINI_SWE_AGENT_EXCLUDE_NEWER = "2026-10-01T00:00:00Z"
MINI_SWE_AGENT_CONFIG_FILE = ".app/mini_swe_agent/config.yaml"
# The helper source travels in the environment, not as a launch file: a
# protected run rewrites the gateway URL in every launch file and expects the
# relay URL in each one.
MINI_SWE_AGENT_HELPER_ENV = "APP_MINI_SWE_AGENT_HELPER"
# mini stops a model shell command after 30 s by default, which cuts off test
# suites and builds.
MINI_SWE_AGENT_COMMAND_TIMEOUT = 600
# mini has no effort names of its own: the config hands the level to LiteLLM in
# ``extra_body``, which sends it to the gateway unchanged. These are Skynet's
# levels; the launch refuses any other, so a run never gets a level the user
# did not pick.
MINI_SWE_AGENT_EFFORTS = ("none", "minimal", "low", "medium", "high", "xhigh", "max")
# mini runs on LiteLLM, which routes an ``openai/`` model to the ``api_base``
# in the config file; the gateway receives the id after the prefix.
_LITELLM_PREFIX = "openai/"
# ``uv tool install`` and the uv installer both put their binaries here.
_MINI_PATH = 'export PATH="$HOME/.local/bin:$PATH"'
_TOOL_PYTHON = f'"$(uv tool dir 2>/dev/null)/{MINI_SWE_AGENT_PACKAGE}/bin/python"'
# mini has no ``--version`` option, so the probe asks the tool's own Python
# for the installed distribution. It reads package metadata only: importing
# ``minisweagent`` would create its global config folder. A tool with another
# LiteLLM prints that version instead, so a cached tool with the pinned mini
# but a different LiteLLM fails the check. ``-I`` keeps the workspace off
# ``sys.path``. ``--version`` lands in ``sys.argv`` and is ignored.
_PROBE = f"{_TOOL_PYTHON} -I -c " + shlex.quote(
    'import importlib.metadata as m; l = m.version("litellm"); '
    f'print(m.version("{MINI_SWE_AGENT_PACKAGE}") if l == "{MINI_SWE_AGENT_LITELLM_VERSION}" else "litellm " + l)'
)
# Runs on the tool's Python with ``-I -c``, so no workspace module shadows the
# standard library. ``prepare`` writes the task into a config file, because
# AGENTS.md plus the prompt can pass Linux's 128 KiB limit on one argument. It
# prints the absolute path of mini's built-in ``mini`` config, because the bare
# spec ``mini`` loads a ``mini.yaml`` from the workspace first. ``answer``
# prints the trajectory's submission, else the last assistant text. It prints
# nothing when mini did not submit, so the bridge records a failed run.
_HELPER = r'''"""Prepare a mini-swe-agent run and print its final answer."""

import importlib.util
import json
import os
import pathlib
import re
import sys

# JSON is YAML, but PyYAML rejects or folds these characters inside a
# double-quoted string, so they go in as escapes.
_YAML_UNSAFE = re.compile("[\x7f-\x9f  ﻿￾￿]")


def _read(path):
    return pathlib.Path(path).read_text(encoding="utf-8", errors="replace")


def prepare(folder):
    task = _read(os.environ["__PROMPT_ENV__"])
    if os.path.isfile("AGENTS.md"):
        task = _read("AGENTS.md") + "\n\n" + task
    text = json.dumps({"run": {"task": task.rstrip("\n")}}, ensure_ascii=False)
    text = _YAML_UNSAFE.sub(lambda match: "\\u%04x" % ord(match.group()), text)
    pathlib.Path(folder, "task.yaml").write_text(text + "\n", encoding="utf-8")
    origin = importlib.util.find_spec("minisweagent").origin
    print(pathlib.Path(origin).parent / "config" / "mini.yaml")


def _text(content):
    if isinstance(content, str):
        return content
    if isinstance(content, list):
        return "".join(
            part.get("text") or "" for part in content if isinstance(part, dict) and part.get("type") == "text"
        )
    return ""


def answer(path):
    try:
        data = json.loads(_read(path))
    except (OSError, ValueError):
        return
    info = data.get("info") or {}
    if info.get("exit_status") != "Submitted":
        return
    text = str(info.get("submission") or "")
    for message in reversed(data.get("messages") or []):
        if text.strip():
            break
        if isinstance(message, dict) and message.get("role") == "assistant":
            text = _text(message.get("content"))
    print(text.strip() or "mini-swe-agent submitted no final output.")


if __name__ == "__main__":
    {"prepare": prepare, "answer": answer}[sys.argv[1]](sys.argv[2])
'''.replace("__PROMPT_ENV__", ENV_PROMPT_FILE)
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
    # The OpenAI client falls back to these when no base URL is set.
    "OPENAI_BASE_URL": "",
    "OPENAI_API_BASE": "",
    # LiteLLM loads a ``.env`` from the workspace in its default DEV mode.
    "LITELLM_MODE": "PRODUCTION",
    # A protected sandbox has no route to the internet: LiteLLM keeps its
    # bundled cost map instead of fetching one.
    "LITELLM_LOCAL_MODEL_COST_MAP": "True",
}


def _mini_install() -> str:
    """Return the command that pins mini-swe-agent to :data:`MINI_SWE_AGENT_VERSION` and logs the version that will run.

    Harbor installs the latest uv and lets every dependency float; this pins
    uv, the package and LiteLLM, and resolves the other dependencies as of
    :data:`MINI_SWE_AGENT_EXCLUDE_NEWER`.

    Returns:
        The shell command.
    """
    check = _version_check(_PROBE, MINI_SWE_AGENT_VERSION)
    return (
        f"{_MINI_PATH}; {check} || "
        "{ { command -v uv >/dev/null 2>&1 || "
        f"curl -LsSf https://astral.sh/uv/{MINI_SWE_AGENT_UV_VERSION}/install.sh | sh; }} && "
        f"uv tool install --force --python {MINI_SWE_AGENT_PYTHON} --exclude-newer {MINI_SWE_AGENT_EXCLUDE_NEWER} "
        f"--with litellm=={MINI_SWE_AGENT_LITELLM_VERSION} "
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
        # mini's built-in config sets LiteLLM ``drop_params``, which drops a
        # top-level ``reasoning_effort`` for a model id LiteLLM knows as
        # non-reasoning (``gpt-4o``). ``extra_body`` survives it; LiteLLM still
        # sends it as ``reasoning.effort`` on its ``/responses`` route.
        model_kwargs["extra_body"] = {"reasoning_effort": reasoning_effort}
    config = {
        # Skynet's run budget bounds the spend. LiteLLM has no prices for
        # gateway model ids, and mini's default $3 limit would stop a run early.
        "agent": {"cost_limit": 0},
        "environment": {"timeout": MINI_SWE_AGENT_COMMAND_TIMEOUT},
        "model": {"model_class": "litellm", "cost_tracking": "ignore_errors", "model_kwargs": model_kwargs},
    }
    return json.dumps(config, indent=2) + "\n"


def _mini_launch(model: str, gateway: GatewayConfig, *, reasoning_effort: str | None = None) -> HarnessLaunch:
    """Build the launch for mini-swe-agent (:data:`MINI_SWE_AGENT_PACKAGE`, pinned to :data:`MINI_SWE_AGENT_VERSION`).

    Each run points ``MSWEA_GLOBAL_CONFIG_DIR`` at a fresh folder outside the
    workspace, so mini loads no inherited ``.env``, and its task file and
    trajectory never land in the files the run hands back. An EXIT trap removes
    the folder. mini has no JSON event stream and prints its whole transcript,
    so the transcript goes to stderr and the helper prints the answer from the
    trajectory. The command keeps mini's exit status.

    Args:
        model: Target model id.
        gateway: Gateway URL and key.
        reasoning_effort: The user's effort for the model, or ``None`` for the model's default.

    Returns:
        The launch.

    Raises:
        ServiceError: When ``reasoning_effort`` is not in :data:`MINI_SWE_AGENT_EFFORTS`.
    """
    if reasoning_effort and reasoning_effort not in MINI_SWE_AGENT_EFFORTS:
        raise ServiceError(
            f"The mini-swe-agent harness does not support the effort level {reasoning_effort!r}. "
            f"Supported levels: {', '.join(MINI_SWE_AGENT_EFFORTS)}."
        )
    helper = f'"$mini_py" -I -c "${MINI_SWE_AGENT_HELPER_ENV}"'
    run_command = (
        f"{_MINI_PATH} && "
        'export MSWEA_GLOBAL_CONFIG_DIR="$(mktemp -d)" && '
        "trap 'rm -rf \"$MSWEA_GLOBAL_CONFIG_DIR\"' EXIT && "
        f"mini_py={_TOOL_PYTHON} && "
        f'mini_base="$({helper} prepare "$MSWEA_GLOBAL_CONFIG_DIR")" && '
        "{ mini_status=0; "
        f'OPENAI_API_KEY="${ENV_API_KEY}" '
        f'mini-swe-agent --yolo --exit-immediately --model "{_LITELLM_PREFIX}${ENV_MODEL}" '
        f'-c "$mini_base" -c {MINI_SWE_AGENT_CONFIG_FILE} -c "$MSWEA_GLOBAL_CONFIG_DIR/task.yaml" '
        '-o "$MSWEA_GLOBAL_CONFIG_DIR/trajectory.json" < /dev/null >&2 || mini_status=$?; '
        # ``(exit ...)`` sets the status without ending the shell before the trap.
        f'{helper} answer "$MSWEA_GLOBAL_CONFIG_DIR/trajectory.json"; (exit "$mini_status"); }}'
    )
    env = _base_env(model, gateway)
    env.update(
        {
            # Skips mini's interactive first-run setup, which would wait on stdin.
            "MSWEA_CONFIGURED": "true",
            "MSWEA_SILENT_STARTUP": "1",
            "MSWEA_COST_TRACKING": "ignore_errors",
            MINI_SWE_AGENT_HELPER_ENV: _HELPER,
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
        supported_efforts=MINI_SWE_AGENT_EFFORTS,
    )
)
