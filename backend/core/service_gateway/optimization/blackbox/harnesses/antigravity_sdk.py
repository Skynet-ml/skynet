"""Google Antigravity SDK harness: a pinned uv script that runs one SDK agent turn through the gateway.

Adapted from Harbor 0.25.0 (Apache-2.0), agents/installed/antigravity_sdk.
"""

from __future__ import annotations

from urllib.parse import urlsplit

from .....exceptions import ServiceError
from .....models.blackbox import BLACKBOX_HARNESS_ANTIGRAVITY_SDK
from ..harness_bridge import parse_pi_output
from . import HarnessSpec, register
from .base import ENV_API_KEY, ENV_MODEL, ENV_PROMPT_FILE, GatewayConfig, HarnessLaunch, _base_env, _version_check

HARNESS_ID = BLACKBOX_HARNESS_ANTIGRAVITY_SDK
ANTIGRAVITY_SDK_VERSION = "0.1.20"
# Exact pins from Harbor's ``antigravity_sdk_runner.py.lock``. Harbor caps
# cryptography below 47 and adds fastapi because the SDK needs both at run time.
_SCRIPT_PINS = (
    f"google-antigravity=={ANTIGRAVITY_SDK_VERSION}",
    "cryptography==46.0.7",
    "fastapi==0.136.3",
)
# The same uv release the native engines install, so one pin covers the box.
_UV_PIN = "uv==0.9.13"
RUNNER_FILE = ".app/antigravity/run_agent.py"
_UV_PATH = 'PATH="$HOME/.local/bin:$PATH"'
_RUNNER = f"uv run --quiet --script {RUNNER_FILE}"
ENV_THINKING_LEVEL = "ANTIGRAVITY_THINKING_LEVEL"
# Gemini has no thinking-off switch, so ``none`` maps to the lowest level.
_THINKING_LEVELS = {
    "none": "MINIMAL",
    "minimal": "MINIMAL",
    "low": "LOW",
    "medium": "MEDIUM",
    "high": "HIGH",
    "xhigh": "EXTRA_HIGH",
    "max": "EXTRA_HIGH",
}
# Empty values override any key the host shell leaks into the box, so the SDK
# cannot fall back to a direct Gemini or Vertex route.
_NO_DIRECT_KEYS = {"GEMINI_API_KEY": "", "GOOGLE_API_KEY": "", "GOOGLE_APPLICATION_CREDENTIALS": ""}
# A protected sandbox has no route to the internet: uv must use its cache and
# the SDK must not phone home.
_PROTECTED_ENV = {"UV_OFFLINE": "1", "UV_NO_PROGRESS": "1", "DO_NOT_TRACK": "1", **_NO_DIRECT_KEYS}
_URL_FORBIDDEN = frozenset("\"'\\")

_RUNNER_SCRIPT = """#!/usr/bin/env -S uv run --script
# /// script
# requires-python = ">=3.12,<3.15"
# dependencies = [
{dependencies}
# ]
# ///
\"\"\"Run one Google Antigravity SDK agent turn through the Skynet model gateway.

Adapted from Harbor 0.25.0 (Apache-2.0), agents/installed/antigravity_sdk_runner.
\"\"\"

import asyncio
import json
import logging
import os
import sys
from importlib.metadata import version
from pathlib import Path

from google.antigravity import Agent, LocalAgentConfig
from google.antigravity.hooks import policy
from google.antigravity.models import GeminiAPIEndpoint, GeminiModelOptions, ModelTarget, ThinkingLevel

GATEWAY_URL = {gateway_url!r}
STATE_DIR = Path({state_dir!r})
DIRECT_ROUTES = ("GEMINI_API_KEY", "GOOGLE_API_KEY", "GOOGLE_APPLICATION_CREDENTIALS", "GOOGLE_GENAI_USE_VERTEXAI")


def _count(usage, field):
    \"\"\"Return one SDK usage counter, or 0 when the SDK did not report it.\"\"\"
    return int(getattr(usage, field, None) or 0)


async def _run():
    \"\"\"Send the prompt to the agent and print one Pi-shaped ``message_end`` event.\"\"\"
    api_key = os.environ.get({env_api_key!r}, "")
    model = os.environ.get({env_model!r}, "")
    if not GATEWAY_URL or not api_key or not model:
        sys.exit("The Antigravity SDK harness needs the gateway URL, the gateway token and a model.")
    for name in DIRECT_ROUTES:
        os.environ.pop(name, None)
    # The SDK's agent process sends every Gemini call to this base URL.
    os.environ["GOOGLE_GEMINI_BASE_URL"] = GATEWAY_URL
    prompt = Path(os.environ[{env_prompt_file!r}]).read_text(encoding="utf-8")
    level = os.environ.get({env_thinking_level!r})
    thinking = getattr(ThinkingLevel, level) if level else None
    options = GeminiModelOptions(thinking_level=thinking) if thinking is not None else GeminiModelOptions()
    workspace = Path.cwd().resolve()
    for name in ("session", "app_data"):
        (STATE_DIR / name).mkdir(parents=True, exist_ok=True)
    config = LocalAgentConfig(
        model=ModelTarget(name=model, endpoint=GeminiAPIEndpoint(api_key=api_key, options=options)),
        api_key=api_key,
        workspaces=[str(workspace)],
        save_dir=str((STATE_DIR / "session").resolve()),
        app_data_dir=str((STATE_DIR / "app_data").resolve()),
        mcp_servers=[],
        policies=[policy.allow_all()],
    )
    # Harbor clears the eval preset's own thinking level only when the user picked one.
    config = config.eval(thinking_level=None) if thinking is not None else config.eval()
    async with Agent(config) as agent:
        response = await agent.chat(prompt)
        text = await response.text()
        await response.resolve()
        usage = getattr(agent.conversation, "total_usage", None)
    output = _count(usage, "candidates_token_count") + _count(usage, "thoughts_token_count")
    event = {{
        "type": "message_end",
        "message": {{
            "role": "assistant",
            "content": [{{"type": "text", "text": text or ""}}],
            "usage": {{"input": _count(usage, "prompt_token_count"), "output": output}},
        }},
    }}
    print(json.dumps(event), flush=True)


def main():
    \"\"\"Print the SDK version for ``--version``; otherwise run the agent.\"\"\"
    if sys.argv[1:] == ["--version"]:
        print(version("google-antigravity"))
        return
    logging.basicConfig(stream=sys.stderr, level=logging.ERROR)
    asyncio.run(_run())


if __name__ == "__main__":
    main()
"""


def _gateway_url(gateway: GatewayConfig) -> str:
    """Return the gateway base URL the SDK must call, or refuse the launch.

    Harbor falls back to a direct ``GEMINI_API_KEY``; Skynet never does, so a
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
        raise ServiceError("The Antigravity SDK harness needs an absolute http(s) gateway URL.")
    parts = urlsplit(url)
    try:
        port_ok = parts.port is None or parts.port > 0
    except ValueError:
        port_ok = False
    if parts.scheme not in ("http", "https") or not parts.hostname or not port_ok or parts.fragment:
        raise ServiceError("The Antigravity SDK harness needs an absolute http(s) gateway URL.")
    if parts.username or parts.password:
        raise ServiceError("The Antigravity SDK gateway URL must not embed credentials.")
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
        gateway_url=gateway_url,
        state_dir=".app/antigravity",
        env_api_key=ENV_API_KEY,
        env_model=ENV_MODEL,
        env_prompt_file=ENV_PROMPT_FILE,
        env_thinking_level=ENV_THINKING_LEVEL,
    )


def _antigravity_install() -> str:
    """Return the command that installs uv if needed and warms the pinned script environment.

    Returns:
        The shell command.
    """
    return (
        f"export {_UV_PATH}; {_version_check(_RUNNER, ANTIGRAVITY_SDK_VERSION)} || "
        "{ command -v uv > /dev/null 2>&1 || "
        f"python3 -m pip install --disable-pip-version-check --no-deps --user {_UV_PIN}; "
        f"{_RUNNER} --version; }}"
    )


def _antigravity_launch(model: str, gateway: GatewayConfig, *, reasoning_effort: str | None = None) -> HarnessLaunch:
    """Build the launch for the Google Antigravity SDK (``google-antigravity``).

    Args:
        model: Target model id as the gateway knows it.
        gateway: Gateway URL and key; the SDK sends every model call there.
        reasoning_effort: The user's effort for the model, or ``None`` for the SDK default.

    Returns:
        The launch.
    """
    env = _base_env(model, gateway)
    env.update(_NO_DIRECT_KEYS)
    env["UV_NO_PROGRESS"] = "1"
    level = _THINKING_LEVELS.get((reasoning_effort or "").lower())
    if level:
        env[ENV_THINKING_LEVEL] = level
    return HarnessLaunch(
        instructions_file="AGENTS.md",
        install_command=_antigravity_install(),
        files={RUNNER_FILE: _runner_script(_gateway_url(gateway))},
        run_command=f"{_UV_PATH} {_RUNNER}",
        env=env,
        parse_output=parse_pi_output,
    )


SPEC = register(
    HarnessSpec(
        id=HARNESS_ID,
        launch=_antigravity_launch,
        binary=_RUNNER,
        version=ANTIGRAVITY_SDK_VERSION,
        output_format="pi",
        protected_env=_PROTECTED_ENV,
    )
)
