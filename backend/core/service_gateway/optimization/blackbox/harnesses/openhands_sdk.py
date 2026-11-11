"""OpenHands SDK harness: a pinned uv script that runs one OpenHands SDK agent conversation through the gateway.

Adapted from Harbor 0.25.0 (Apache-2.0), agents/installed/openhands_sdk.
"""

from __future__ import annotations

import shlex
from urllib.parse import urlsplit

from .....exceptions import ServiceError
from .....models.blackbox import BLACKBOX_HARNESS_OPENHANDS_SDK
from ..harness_bridge import parse_pi_output
from . import HarnessSpec, register
from .base import ENV_API_KEY, ENV_MODEL, ENV_PROMPT_FILE, GatewayConfig, HarnessLaunch, _base_env, _version_check

HARNESS_ID = BLACKBOX_HARNESS_OPENHANDS_SDK
OPENHANDS_SDK_VERSION = "1.61.0"
# Harbor installs these three packages; the SDK and its tools ship in lockstep.
# fastapi reuses the Antigravity SDK pin so one cached release covers the box.
_SCRIPT_PINS = (
    f"openhands-sdk=={OPENHANDS_SDK_VERSION}",
    f"openhands-tools=={OPENHANDS_SDK_VERSION}",
    "fastapi==0.136.3",
)
# The same uv release the native engines install, so one pin covers the box.
_UV_PIN = "uv==0.9.13"
# openhands-sdk 1.61.0 requires Python 3.12 or newer.
_REQUIRES_PYTHON = ">=3.12,<3.15"
RUNNER_FILE = ".app/openhands_sdk/run_agent.py"
_UV_PATH = 'PATH="$HOME/.local/bin:$PATH"'
_RUNNER = f"uv run --quiet --script {RUNNER_FILE}"
# The native proposer bootstrap checks and installs the harness before any
# launch file is in the workspace, so the probe resolves the script's pins
# without the runner file. ``--version`` lands in ``sys.argv`` and is ignored.
_SDK_ENV = (
    f"--no-project --python {shlex.quote(_REQUIRES_PYTHON)} "
    + " ".join(f"--with {pin}" for pin in _SCRIPT_PINS)
    + " python -c "
    + shlex.quote('import importlib.metadata as m; print(m.version("openhands-sdk"))')
)
# The check never downloads: in open mode the install has warmed the uv cache,
# and a protected image must already carry it.
_PROBE = f"uv run --quiet --offline {_SDK_ENV}"
_WARM = f"uv run --quiet {_SDK_ENV}"
ENV_REASONING_EFFORT = "OPENHANDS_SDK_REASONING_EFFORT"
INSTRUCTIONS_FILE = "AGENTS.md"
# The provider-neutral values that ``LLM.reasoning_effort`` documents in
# openhands-sdk 1.61.0; the SDK hands them to LiteLLM unchanged.
EFFORTS = ("none", "minimal", "low", "medium", "high", "xhigh", "max")
# Empty values override any key the host shell leaks into the box, so LiteLLM
# cannot fall back to a direct provider route. The runner sets its own
# ``LLM_API_KEY`` and ``LLM_BASE_URL`` from the gateway inside the process.
# ``LMNR_PROJECT_API_KEY`` and the OTEL endpoints would turn on the SDK's
# Laminar trace export (``should_enable_observability``); an empty value is off.
_NO_DIRECT_KEYS = {
    "ANTHROPIC_API_KEY": "",
    "OPENAI_API_KEY": "",
    "GEMINI_API_KEY": "",
    "GOOGLE_API_KEY": "",
    "OPENROUTER_API_KEY": "",
    "LLM_API_KEY": "",
    "LLM_BASE_URL": "",
    "LMNR_PROJECT_API_KEY": "",
    "OTEL_ENDPOINT": "",
    "OTEL_EXPORTER_OTLP_TRACES_ENDPOINT": "",
    "OTEL_EXPORTER_OTLP_ENDPOINT": "",
}
_QUIET_ENV = {
    "UV_NO_PROGRESS": "1",
    "LITELLM_LOCAL_MODEL_COST_MAP": "True",
    "DO_NOT_TRACK": "1",
    "ANONYMIZED_TELEMETRY": "false",
    "OPENHANDS_SUPPRESS_BANNER": "1",
}
# A protected sandbox has no route to the internet: uv must use its cache and
# the SDK must not phone home.
_PROTECTED_ENV = {"UV_OFFLINE": "1", **_QUIET_ENV, **_NO_DIRECT_KEYS}
_URL_FORBIDDEN = frozenset("\"'\\")

_RUNNER_SCRIPT = """#!/usr/bin/env -S uv run --script
# /// script
# requires-python = "{requires_python}"
# dependencies = [
{dependencies}
# ]
# ///
\"\"\"Run one OpenHands SDK agent conversation through the Skynet model gateway.

Adapted from Harbor 0.25.0 (Apache-2.0), agents/installed/openhands_sdk_runner.
\"\"\"

import atexit
import json
import os
import shutil
import sys
import tempfile
from pathlib import Path

STATE_DIR = Path({state_dir!r})
# The SDK reads saved LLM profiles, SOUL.md and other user state from
# ~/.openhands, and freezes that folder when it is imported. A saved vision
# profile adds a tool that calls the profile's own base URL and key, not the
# gateway, so the SDK gets an empty folder that goes away with the run.
STATE_DIR.mkdir(parents=True, exist_ok=True)
PERSISTENCE_DIR = tempfile.mkdtemp(prefix="home-", dir=STATE_DIR.resolve())
atexit.register(shutil.rmtree, PERSISTENCE_DIR, True)
os.environ["OH_PERSISTENCE_DIR"] = PERSISTENCE_DIR

from openhands.sdk import LLM, Agent, AgentContext, Conversation, Tool  # noqa: E402
from openhands.sdk.conversation.impl import local_conversation  # noqa: E402
from openhands.sdk.event import ActionEvent, MessageEvent  # noqa: E402
from openhands.tools.file_editor import FileEditorTool  # noqa: E402
from openhands.tools.task_tracker import TaskTrackerTool  # noqa: E402
from openhands.tools.terminal import TerminalTool  # noqa: E402

GATEWAY_URL = {gateway_url!r}
# LiteLLM sends an ``openai/`` model to the OpenAI-style base URL as is.
LITELLM_PREFIX = "openai/"
DIRECT_ROUTES = (
    "ANTHROPIC_API_KEY",
    "OPENAI_API_KEY",
    "GEMINI_API_KEY",
    "GOOGLE_API_KEY",
    "OPENROUTER_API_KEY",
    "LMNR_PROJECT_API_KEY",
    "OTEL_ENDPOINT",
    "OTEL_EXPORTER_OTLP_TRACES_ENDPOINT",
    "OTEL_EXPORTER_OTLP_ENDPOINT",
)
INSTRUCTIONS_FILE = Path({instructions_file!r})


def _no_ambient_plugins(*args, **kwargs):
    \"\"\"Return no plugins, so the SDK loads none from the home or workspace plugin folders.\"\"\"
    return {{}}


# The SDK always scans ~/.agents/plugins, ~/.openhands/plugins and the
# workspace plugin folders, and has no switch to stop it. A found plugin can add
# skills, MCP servers and shell hooks, so the runner replaces the scan.
local_conversation.load_available_plugins = _no_ambient_plugins


def _instructions():
    \"\"\"Return the workspace instructions file text, or ``None`` when it is missing or empty.

    The SDK reads AGENTS.md only with ``load_project_skills``, which also pulls in
    other workspace skill files, so the runner passes this one file itself.
    \"\"\"
    if not INSTRUCTIONS_FILE.is_file():
        return None
    return INSTRUCTIONS_FILE.read_text(encoding="utf-8") or None


def _text(content):
    \"\"\"Join the text parts of one SDK message content.\"\"\"
    if isinstance(content, list):
        return "\\n".join(part.text for part in content if getattr(part, "text", None))
    return str(content or "")


def _final_answer(events):
    \"\"\"Return the agent's last message or finish text, or an empty string.\"\"\"
    answer = ""
    for event in events:
        if isinstance(event, MessageEvent) and event.source == "agent" and event.llm_message:
            answer = _text(getattr(event.llm_message, "content", None)) or answer
        elif isinstance(event, ActionEvent) and isinstance(getattr(event.action, "message", None), str):
            answer = event.action.message or answer
    return answer


def _run(prompt, model, api_key):
    \"\"\"Run the conversation and return the final answer and the token usage.\"\"\"
    effort = os.environ.get({env_reasoning_effort!r})
    # Skynet's CLI and BYOK routes reject /responses, so the SDK must not pick
    # it for gpt-5* models in "auto" mode.
    settings = {{
        "model": LITELLM_PREFIX + model,
        "base_url": GATEWAY_URL,
        "api_key": api_key,
        "usage_id": "agent",
        "api_mode": "chat",
    }}
    if effort:
        settings["reasoning_effort"] = effort
        # LiteLLM's GPT-5 config silently drops ``minimal`` and ``xhigh`` for some
        # ``gpt-5`` ids. The extra body field reaches the gateway unchanged.
        settings["litellm_extra_body"] = {{"reasoning_effort": effort}}
    llm = LLM(**settings)
    tools = [Tool(name=TerminalTool.name), Tool(name=FileEditorTool.name), Tool(name=TaskTrackerTool.name)]
    # The candidate is the optimization target, so it goes in as raw system
    # prompt text. A trigger-less skill would land in <REPO_CONTEXT>, which the
    # SDK marks untrusted and limits to style and convention guidance. No skills
    # and no MCP servers.
    agent = Agent(llm=llm, tools=tools, agent_context=AgentContext(system_message_suffix=_instructions()))
    # The default visualizer prints rich text to stdout around the Pi event.
    conversation = Conversation(agent=agent, workspace=str(Path.cwd().resolve()), visualizer=None)
    conversation.send_message(prompt)
    conversation.run()
    usage = llm.metrics.accumulated_token_usage
    tokens = {{
        "input": int(getattr(usage, "prompt_tokens", 0) or 0),
        "output": int(getattr(usage, "completion_tokens", 0) or 0),
    }}
    return _final_answer(conversation.state.events), tokens


def main():
    \"\"\"Run the agent and print one Pi-shaped ``message_end`` event.\"\"\"
    api_key = os.environ.get({env_api_key!r}, "")
    model = os.environ.get({env_model!r}, "")
    if not GATEWAY_URL or not api_key or not model:
        sys.exit("The OpenHands SDK harness needs the gateway URL, the gateway token and a model.")
    for name in DIRECT_ROUTES:
        os.environ.pop(name, None)
    # The SDK's own settings read these names; both point at the gateway.
    os.environ["LLM_API_KEY"] = api_key
    os.environ["LLM_BASE_URL"] = GATEWAY_URL
    prompt = Path(os.environ[{env_prompt_file!r}]).read_text(encoding="utf-8")
    # Tool scratch files land here and go away with the run.
    with tempfile.TemporaryDirectory(dir=STATE_DIR.resolve()) as scratch:
        os.environ["TMPDIR"] = scratch
        tempfile.tempdir = scratch
        try:
            text, tokens = _run(prompt, model, api_key)
        finally:
            tempfile.tempdir = None
    event = {{
        "type": "message_end",
        "message": {{"role": "assistant", "content": [{{"type": "text", "text": text}}], "usage": tokens}},
    }}
    print(json.dumps(event), flush=True)


if __name__ == "__main__":
    main()
"""


def _gateway_url(gateway: GatewayConfig) -> str:
    """Return the gateway base URL the SDK must call, or refuse the launch.

    Harbor falls back to a direct provider key; Skynet never does, so a
    missing or malformed gateway URL stops the launch here.

    Args:
        gateway: Gateway URL and key.

    Returns:
        The validated gateway URL.

    Raises:
        ServiceError: When the URL is empty, not absolute http(s), embeds
            credentials, or has characters that cannot sit in the runner literal.
    """
    url = gateway.url
    if not url or any(char.isspace() or char in _URL_FORBIDDEN for char in url):
        raise ServiceError("The OpenHands SDK harness needs an absolute http(s) gateway URL.")
    parts = urlsplit(url)
    try:
        port_ok = parts.port is None or parts.port > 0
    except ValueError:
        port_ok = False
    if parts.scheme not in ("http", "https") or not parts.hostname or not port_ok or parts.fragment:
        raise ServiceError("The OpenHands SDK harness needs an absolute http(s) gateway URL.")
    if parts.username or parts.password:
        raise ServiceError("The OpenHands SDK gateway URL must not embed credentials.")
    return url


def _runner_script(gateway_url: str) -> str:
    """Return the pinned uv script that runs the SDK against ``gateway_url``.

    The URL sits in the script text, not the env, so the protected relay
    rewrite of generated files points it at the sandbox-local mailbox.

    Args:
        gateway_url: Validated gateway base URL.

    Returns:
        The script source.
    """
    return _RUNNER_SCRIPT.format(
        dependencies="\n".join(f'#   "{pin}",' for pin in _SCRIPT_PINS),
        requires_python=_REQUIRES_PYTHON,
        gateway_url=gateway_url,
        state_dir=".app/openhands_sdk",
        env_api_key=ENV_API_KEY,
        env_model=ENV_MODEL,
        env_prompt_file=ENV_PROMPT_FILE,
        env_reasoning_effort=ENV_REASONING_EFFORT,
        instructions_file=INSTRUCTIONS_FILE,
    )


def _openhands_sdk_install() -> str:
    """Return the command that installs uv if needed and warms the pinned SDK environment.

    Returns:
        The shell command.
    """
    return (
        f"export {_UV_PATH}; {_version_check(_PROBE, OPENHANDS_SDK_VERSION)} || "
        "{ command -v uv > /dev/null 2>&1 || "
        f"python3 -m pip install --disable-pip-version-check --no-deps --user {_UV_PIN}; "
        f"{_WARM} > /dev/null; }}"
    )


def _openhands_sdk_launch(model: str, gateway: GatewayConfig, *, reasoning_effort: str | None = None) -> HarnessLaunch:
    """Build the launch for the OpenHands SDK (``openhands-sdk`` with ``openhands-tools``).

    Args:
        model: Target model id as the gateway knows it.
        gateway: Gateway URL and key; the SDK sends every model call there.
        reasoning_effort: The user's effort for the model, or ``None`` for the SDK default.

    Returns:
        The launch.

    Raises:
        ServiceError: When ``reasoning_effort`` is not a level the SDK supports.
    """
    if reasoning_effort and reasoning_effort not in EFFORTS:
        raise ServiceError(
            f"The OpenHands SDK harness does not support the effort level {reasoning_effort!r}. "
            f"Supported levels: {', '.join(EFFORTS)}."
        )
    env = _base_env(model, gateway)
    env.update(_NO_DIRECT_KEYS)
    env.update(_QUIET_ENV)
    if reasoning_effort:
        env[ENV_REASONING_EFFORT] = reasoning_effort
    return HarnessLaunch(
        instructions_file=INSTRUCTIONS_FILE,
        install_command=_openhands_sdk_install(),
        files={RUNNER_FILE: _runner_script(_gateway_url(gateway))},
        run_command=f"{_UV_PATH} {_RUNNER}",
        env=env,
        parse_output=parse_pi_output,
    )


SPEC = register(
    HarnessSpec(
        id=HARNESS_ID,
        launch=_openhands_sdk_launch,
        binary=_PROBE,
        version=OPENHANDS_SDK_VERSION,
        output_format="pi",
        protected_env=_PROTECTED_ENV,
        position=56,
        supported_efforts=EFFORTS,
    )
)
