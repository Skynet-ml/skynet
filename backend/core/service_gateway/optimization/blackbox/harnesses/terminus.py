"""Terminus 2 harness: a pinned uv script that runs Harbor's own tmux agent through the gateway.

Adapted from Harbor 0.25.0 (Apache-2.0), agents/terminus_2. Terminus is not an
installed CLI: Harbor drives a tmux terminal from model calls inside its own
process. Harbor 0.25.0 implements only ``terminus-2``; the ``terminus`` and
``terminus-1`` names have no agent class there, so Skynet offers Terminus 2
alone. The runner imports the pinned Harbor package instead of a copy of its
4,000 lines, and gives it the run's own box as the terminal. Harbor reads the
model key from its own provider env names; Skynet passes the gateway token
directly and sends every model call to the gateway.
"""

from __future__ import annotations

import shlex
from urllib.parse import urlsplit

from .....exceptions import ServiceError
from .....models.blackbox import BLACKBOX_HARNESS_TERMINUS
from ..harness_bridge import parse_pi_output
from . import HarnessSpec, register
from .base import GatewayConfig, HarnessLaunch, _base_env, _version_check

HARNESS_ID = BLACKBOX_HARNESS_TERMINUS
HARBOR_VERSION = "0.25.0"
# ``litellm`` and ``openai`` carry every model call; these are the exact
# releases in Harbor 0.25.0's ``uv.lock``.
_SCRIPT_PINS = (
    f"harbor=={HARBOR_VERSION}",
    "litellm==1.93.0",
    "openai==2.24.0",
)
# The same uv release the native engines install, so one pin covers the box.
_UV_PIN = "uv==0.9.13"
_REQUIRES_PYTHON = ">=3.12,<3.15"
RUNNER_FILE = ".app/terminus/run_agent.py"
_UV_PATH = 'PATH="$HOME/.local/bin:$PATH"'
_RUNNER = f"uv run --quiet --script {RUNNER_FILE}"
# The native proposer bootstrap checks and installs the harness before any
# launch file is in the workspace, so the probe resolves the script's pins
# without the runner file. ``--version`` lands in ``sys.argv`` and is ignored.
_HARBOR_ENV = (
    f"--no-project --python {shlex.quote(_REQUIRES_PYTHON)} "
    + " ".join(f"--with {pin}" for pin in _SCRIPT_PINS)
    + " python -c "
    + shlex.quote('import importlib.metadata as m; print(m.version("harbor"))')
)
# The check never downloads: in open mode the install has warmed the uv cache,
# and a protected image must already carry it.
_PROBE = f"uv run --quiet --offline {_HARBOR_ENV}"
_WARM = f"uv run --quiet {_HARBOR_ENV}"
ENV_REASONING_EFFORT = "TERMINUS_REASONING_EFFORT"
# The levels ``Terminus2`` accepts and LiteLLM 1.93.0 sends unchanged as
# ``reasoning_effort`` on an ``openai/`` route; Harbor's extra ``default`` is
# not a Skynet effort.
_EFFORTS = frozenset({"none", "minimal", "low", "medium", "high", "xhigh", "max"})
# Empty values override any key the host shell leaks into the box, so LiteLLM
# cannot fall back to a direct provider route.
_NO_DIRECT_KEYS = {
    "OPENAI_API_KEY": "",
    "OPENAI_BASE_URL": "",
    "OPENAI_API_BASE": "",
    "ANTHROPIC_API_KEY": "",
    "GEMINI_API_KEY": "",
    "GOOGLE_API_KEY": "",
    "OPENROUTER_API_KEY": "",
}
# Harbor's PostHog telemetry fires only for its job CLI, and LiteLLM fetches
# its cost map from GitHub at import; both stay off so nothing leaves the box.
_QUIET_ENV = {"HARBOR_TELEMETRY": "0", "LITELLM_LOCAL_MODEL_COST_MAP": "True", "DO_NOT_TRACK": "1"}
# A protected sandbox has no route to the internet: uv must use its cache.
_PROTECTED_ENV = {"UV_OFFLINE": "1", "UV_NO_PROGRESS": "1", **_QUIET_ENV, **_NO_DIRECT_KEYS}
_URL_FORBIDDEN = frozenset("\"'\\")

_RUNNER_SCRIPT = """#!/usr/bin/env -S uv run --script
# /// script
# requires-python = "{requires_python}"
# dependencies = [
{dependencies}
# ]
# ///
\"\"\"Run Harbor's Terminus 2 agent on one task through the Skynet model gateway.

Adapted from Harbor 0.25.0 (Apache-2.0), agents/terminus_2.
\"\"\"

import asyncio
import json
import logging
import os
import shutil
import subprocess
import sys
import tempfile
from pathlib import Path

from harbor.agents.terminus_2 import Terminus2
from harbor.agents.terminus_2.tmux_session import TmuxSession
from harbor.environments.base import ExecResult
from harbor.models.agent.context import AgentContext

GATEWAY_URL = {gateway_url!r}
SECRET_NAMES = ("APP_API_KEY", "SKYNET_API_KEY")


def _env(name):
    \"\"\"Return ``APP_<name>``, or the legacy ``SKYNET_<name>`` when only that is set.\"\"\"
    return os.environ.get("APP_" + name) or os.environ.get("SKYNET_" + name) or ""


class LocalShell:
    \"\"\"The run's own box as the one terminal Terminus drives through tmux.\"\"\"

    session_id = "skynet"
    default_user = None

    def __init__(self, workspace, tmux_dir):
        \"\"\"Run every command in ``workspace`` on a private tmux socket, without the gateway token.\"\"\"
        self.workspace = workspace
        # The tmux server inherits this env, and so does every shell the model
        # drives; the gateway token stays in this process only.
        self.env = {{k: v for k, v in os.environ.items() if k not in SECRET_NAMES}}
        self.env["TMUX_TMPDIR"] = tmux_dir

    async def exec(self, command, cwd=None, env=None, timeout_sec=None, user=None):
        \"\"\"Run ``command`` in bash and return Harbor's result shape. ``user`` is ignored: the box has one user.\"\"\"
        proc = await asyncio.create_subprocess_exec(
            "bash", "-c", command, cwd=cwd or self.workspace, env={{**self.env, **(env or {{}})}},
            stdin=subprocess.DEVNULL, stdout=subprocess.PIPE, stderr=subprocess.PIPE,
        )
        try:
            out, err = await asyncio.wait_for(proc.communicate(), timeout_sec)
        except TimeoutError:
            proc.kill()
            await proc.wait()
            return ExecResult(stdout="", stderr="timed out", return_code=124)
        return ExecResult(
            stdout=out.decode(errors="replace"), stderr=err.decode(errors="replace"), return_code=proc.returncode
        )

    async def is_dir(self, path, user=None):
        \"\"\"Return whether ``path`` is a directory in the box.\"\"\"
        return Path(path).is_dir()


class SkynetTerminus(Terminus2):
    \"\"\"Terminus 2 with its pane log in the run's temp folder instead of Harbor's ``/logs`` mount.\"\"\"

    def __init__(self, *args, pane_log, **kwargs):
        \"\"\"Keep the pane log path for ``setup``.\"\"\"
        super().__init__(*args, **kwargs)
        self._pane_log = pane_log

    async def setup(self, environment):
        \"\"\"Start the tmux session as Harbor's ``setup`` does, minus the asciinema recording.\"\"\"
        self._session = TmuxSession(
            session_name=self.name(), environment=environment, logging_path=self._pane_log,
            local_asciinema_recording_path=None, remote_asciinema_recording_path=None,
            pane_width=self.options.tmux_pane_width, pane_height=self.options.tmux_pane_height,
            extra_env=self._extra_env, user=environment.default_user,
        )
        await self._session.start()


def _answer(chat):
    \"\"\"Return the last assistant turn's analysis, or its raw text when it is not Terminus JSON.\"\"\"
    for message in reversed(chat.messages if chat else []):
        if message.get("role") != "assistant":
            continue
        text = message.get("content") or ""
        try:
            reply = json.loads(text)
        except ValueError:
            return text.strip()
        return str(reply.get("analysis") or "").strip() if isinstance(reply, dict) else text.strip()
    return ""


async def _run(scratch):
    \"\"\"Run the agent to completion and print one Pi-shaped ``message_end`` event.\"\"\"
    api_key, model = _env("API_KEY"), _env("MODEL")
    if not GATEWAY_URL or not api_key or not model:
        sys.exit("The Terminus harness needs the gateway URL, the gateway token and a model.")
    if shutil.which("tmux") is None:
        sys.exit("The Terminus harness needs tmux on PATH; add it to the box image.")
    prompt = Path(_env("PROMPT_FILE")).read_text(encoding="utf-8")
    tmux_dir = scratch / "tmux"
    tmux_dir.mkdir()
    (scratch / "logs").mkdir()
    shell = LocalShell(str(Path.cwd().resolve()), str(tmux_dir))
    agent = SkynetTerminus(
        logs_dir=scratch / "logs",
        # LiteLLM sends an ``openai/`` model to the OpenAI-compatible api_base
        # with the id after the prefix, which is the gateway's model id.
        model_name="openai/" + model,
        api_base=GATEWAY_URL,
        # LiteLLM drops ``reasoning_effort`` for a model its catalog does not
        # list, and a gateway model id is never listed; this keeps it.
        llm_kwargs={{"api_key": api_key, "allowed_openai_params": ["reasoning_effort"]}},
        reasoning_effort=os.environ.get({env_effort!r}) or None,
        record_terminal_session=False,
        pane_log=scratch / "terminus.pane",
    )
    context = AgentContext()
    try:
        await agent.setup(shell)
        await agent.run(prompt, shell, context)
    finally:
        await shell.exec("tmux kill-server", timeout_sec=10)
    event = {{
        "type": "message_end",
        "message": {{
            "role": "assistant",
            "content": [{{"type": "text", "text": _answer(agent._chat)}}],
            "usage": {{"input": context.n_input_tokens or 0, "output": context.n_output_tokens or 0}},
        }},
    }}
    print(json.dumps(event), flush=True)


def main():
    \"\"\"Run the agent in a temp folder outside the workspace, then remove it.\"\"\"
    logging.basicConfig(stream=sys.stderr, level=logging.ERROR)
    # Harbor's own loggers set DEBUG on themselves; the log goes to stderr only.
    logging.getLogger("harbor").setLevel(logging.ERROR)
    logging.getLogger("harbor.utils.logger").setLevel(logging.ERROR)
    scratch = Path(tempfile.mkdtemp(prefix="terminus-"))
    try:
        asyncio.run(_run(scratch))
    finally:
        shutil.rmtree(scratch, ignore_errors=True)


if __name__ == "__main__":
    main()
"""


def _gateway_url(gateway: GatewayConfig) -> str:
    """Return the gateway base URL the runner must call, or refuse the launch.

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
        raise ServiceError("The Terminus harness needs an absolute http(s) gateway URL.")
    parts = urlsplit(url)
    try:
        port_ok = parts.port is None or parts.port > 0
    except ValueError:
        port_ok = False
    if parts.scheme not in ("http", "https") or not parts.hostname or not port_ok or parts.fragment:
        raise ServiceError("The Terminus harness needs an absolute http(s) gateway URL.")
    if parts.username or parts.password:
        raise ServiceError("The Terminus gateway URL must not embed credentials.")
    return url


def _runner_script(gateway_url: str) -> str:
    """Return the pinned uv script that runs Terminus 2 against ``gateway_url``.

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
        env_effort=ENV_REASONING_EFFORT,
    )


def _terminus_install() -> str:
    """Return the command that installs uv if needed and warms the pinned Harbor environment.

    Terminus also needs tmux, and the box image must carry it. Harbor's own
    fallback installs tmux with the package manager as root, which fails for
    the box's user and offline, so the runner stops when tmux is missing.

    Returns:
        The shell command.
    """
    return (
        f"export {_UV_PATH}; {_version_check(_PROBE, HARBOR_VERSION)} || "
        "{ command -v uv > /dev/null 2>&1 || "
        f"python3 -m pip install --disable-pip-version-check --no-deps --user {_UV_PIN}; "
        f"{_WARM} > /dev/null; }}"
    )


def _terminus_launch(model: str, gateway: GatewayConfig, *, reasoning_effort: str | None = None) -> HarnessLaunch:
    """Build the launch for Harbor's Terminus 2 agent (``harbor``, pinned to :data:`HARBOR_VERSION`).

    Args:
        model: Target model id as the gateway knows it.
        gateway: Gateway URL and key; LiteLLM sends every model call there.
        reasoning_effort: The user's effort for the model, or ``None`` for the model default.

    Returns:
        The launch.

    Raises:
        ServiceError: When the effort is not one Terminus 2 accepts.
    """
    if reasoning_effort and reasoning_effort not in _EFFORTS:
        raise ServiceError(f"The Terminus harness does not support the reasoning effort '{reasoning_effort}'.")
    env = _base_env(model, gateway)
    env.update(_NO_DIRECT_KEYS)
    env.update(_QUIET_ENV)
    env["UV_NO_PROGRESS"] = "1"
    if reasoning_effort:
        env[ENV_REASONING_EFFORT] = reasoning_effort
    return HarnessLaunch(
        instructions_file="AGENTS.md",
        install_command=_terminus_install(),
        files={RUNNER_FILE: _runner_script(_gateway_url(gateway))},
        run_command=f"{_UV_PATH} {_RUNNER}",
        env=env,
        parse_output=parse_pi_output,
    )


SPEC = register(
    HarnessSpec(
        id=HARNESS_ID,
        launch=_terminus_launch,
        binary=_PROBE,
        version=HARBOR_VERSION,
        output_format="pi",
        protected_env=_PROTECTED_ENV,
        position=63,
    )
)
