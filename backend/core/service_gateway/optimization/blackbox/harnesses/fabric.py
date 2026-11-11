"""NVIDIA NeMo Fabric harness (``nemo-fabric-runtime`` with its Codex adapter).

Adapted from Harbor 0.25.0 (Apache-2.0), agents/installed/fabric.py: the
task-local venv install of the Fabric runtime, the ``FabricRunPayload`` spec
file handed to ``nemo_fabric.integrations.harbor.runner``, and the read of
``output.response`` from its result file. Skynet pins the runtime and the Codex
adapter, selects Codex with a custom ``app`` provider that points at the model
gateway, and turns telemetry and the update check off.
"""

from __future__ import annotations

import json
import shlex

from .....models.blackbox import BLACKBOX_HARNESS_FABRIC
from ..harness_bridge import parse_plain_output
from . import HarnessSpec, register
from .base import PROVIDER, GatewayConfig, HarnessLaunch, _base_env, _version_check

FABRIC_VERSION = "0.5.0"
# The runtime ships abi3 manylinux wheels for Python >= 3.11. The ``harness``
# extra pins the Codex CLI the adapter drives (openai-codex 0.144.4).
FABRIC_PACKAGES = (
    f"nemo-fabric-runtime=={FABRIC_VERSION}",
    f"nemo-fabric-adapters-codex[harness]=={FABRIC_VERSION}",
)
FABRIC_ADAPTER = "nvidia.fabric.codex"
FABRIC_UV_VERSION = "0.13.7"
FABRIC_PYTHON = "3.12"
FABRIC_CONFIG_FILE = ".app/fabric/config.json"
# The Codex adapter reads a custom provider's key from the variable that
# ``api_key_env`` names; the run fills it from the gateway token.
FABRIC_KEY_ENV = "MODEL_API_KEY"
# The uv installer puts uv here, and the install puts the ``fabric`` launcher here.
_FABRIC_PATH = 'export PATH="$HOME/.local/bin:$PATH"'
# Fabric has no CLI of its own (``fabric-agent-ctl`` has no ``--version``), so
# the install writes this launcher. It answers the pinned version probe with the
# installed runtime release and otherwise runs the venv's Python. It finds the
# venv next to itself, so a later run with another ``$HOME`` still finds it.
_FABRIC_LAUNCHER = (
    "#!/bin/sh\n"
    'venv="$(cd "$(dirname "$0")/../share/app-fabric" && pwd)" || exit 127\n'
    'if [ "$1" = "--version" ]; then\n'
    '  exec "$venv/bin/python" -I -c '
    '\'import importlib.metadata as m; print("fabric", m.version("nemo-fabric-runtime"))\'\n'
    "fi\n"
    'PATH="$venv/bin:$PATH" exec "$venv/bin/python" "$@"\n'
)
# Skynet's effort names that Codex spells differently; Codex knows none,
# minimal, low, medium, high and xhigh, and gets no setting for anything else.
_FABRIC_EFFORTS = {"max": "xhigh"}
_FABRIC_EFFORTS_KNOWN = frozenset({"none", "minimal", "low", "medium", "high", "xhigh"})
# The Codex adapter passes ``OPENAI_API_KEY`` through to Codex, so a protected
# run blanks it and an inherited key never gives Codex a route around the
# gateway. The other flags stop OpenTelemetry export and opt-in usage tracking.
_PROTECTED_ENV = {
    "OPENAI_API_KEY": "",
    "OTEL_SDK_DISABLED": "true",
    "DO_NOT_TRACK": "1",
}
# Runs inside the box with the venv's Python and only the standard library. It
# goes inline on the command line, not as a launch file: the protected relay
# rewrite needs every launch file to carry the gateway URL.
# Fabric's state, Codex's home and the logs go to a fresh directory outside
# the workspace, so they never show up in the scored workspace.
_FABRIC_RUN_SCRIPT = """\
import json
import os
import subprocess
import sys
import tempfile
from pathlib import Path


def main():
    config = json.loads(Path(sys.argv[1]).read_text(encoding="utf-8"))
    prompt = Path(os.environ["APP_PROMPT_FILE"]).read_text(encoding="utf-8")
    state = Path(tempfile.mkdtemp(prefix="app-fabric-"))
    config["environment"]["workspace"] = os.getcwd()
    config["environment"]["artifacts"] = str(state / "artifacts")
    config["runtime"]["artifacts"] = str(state / "artifacts")
    spec = {
        "config": config,
        "config_base_dir": str(state),
        "logs_dir": str(state / "logs"),
        "request": {"input": prompt},
    }
    spec_path = state / "spec.json"
    result_path = state / "result.json"
    spec_path.write_text(json.dumps(spec), encoding="utf-8")
    command = [
        sys.executable, "-I", "-m", "nemo_fabric.integrations.harbor.runner",
        "--spec", str(spec_path), "--result", str(result_path),
    ]
    code = subprocess.run(command, stdout=sys.stderr).returncode
    try:
        result = json.loads(result_path.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        print(f"fabric: the runner wrote no result (exit {code})", file=sys.stderr)
        return code or 1
    failure = result.get("runner_error") or result.get("error")
    if failure or result.get("status") != "succeeded":
        message = failure.get("message") if isinstance(failure, dict) else failure or result.get("status")
        print(f"fabric: {message}", file=sys.stderr)
        return code or 1
    output = result.get("output")
    response = output.get("response") if isinstance(output, dict) else output
    if response is not None:
        print(response if isinstance(response, str) else json.dumps(response))
    return 0


sys.exit(main())
"""


def _fabric_install() -> str:
    """Return the command that pins Fabric to :data:`FABRIC_VERSION` and logs the version that will run.

    Harbor installs an unpinned ``fabric_package`` into a task-local venv; this
    pins uv, Python, the runtime and the Codex adapter instead, and writes the
    launcher inline so it does not depend on when the launch files arrive.

    Returns:
        The shell command.
    """
    venv = '"$HOME/.local/share/app-fabric"'
    venv_python = '"$HOME/.local/share/app-fabric/bin/python"'
    packages = " ".join(shlex.quote(package) for package in FABRIC_PACKAGES)
    return (
        f"{_FABRIC_PATH}; {_version_check('fabric', FABRIC_VERSION)} || "
        "{ { command -v uv >/dev/null 2>&1 || "
        f"curl -LsSf https://astral.sh/uv/{FABRIC_UV_VERSION}/install.sh | sh; }} && "
        f"rm -rf {venv} && uv venv --python {FABRIC_PYTHON} {venv} >/dev/null && "
        f"uv pip install --python {venv_python} {packages} >/dev/null && "
        f'mkdir -p "$HOME/.local/bin" && printf %s {shlex.quote(_FABRIC_LAUNCHER)} > "$HOME/.local/bin/fabric" && '
        'chmod 755 "$HOME/.local/bin/fabric"; } && fabric --version'
    )


def _fabric_config(model: str, gateway: GatewayConfig, reasoning_effort: str | None) -> str:
    """Build the Fabric agent config that runs Codex against the gateway.

    The gateway URL lives in this file rather than on the command line, so a
    protected run's relay rewrite reaches it. The key stays in the environment.

    Args:
        model: Target model id as the gateway knows it.
        gateway: Gateway URL and key.
        reasoning_effort: The user's effort for the model, or ``None`` for Codex's default.

    Returns:
        The JSON text.
    """
    settings: dict[str, object] = {
        # The sandbox is the isolation boundary, and no one is there to approve.
        "sandbox": "danger-full-access",
        "approval_mode": "deny_all",
        "config_overrides": {"analytics.enabled": False, "check_for_update_on_startup": False},
    }
    effort = _FABRIC_EFFORTS.get(reasoning_effort, reasoning_effort) if reasoning_effort else None
    if effort in _FABRIC_EFFORTS_KNOWN:
        settings["reasoning_effort"] = effort
    config = {
        "metadata": {"name": "skynet-fabric"},
        "harness": {"adapter_id": FABRIC_ADAPTER, "resolution": "preinstalled", "settings": settings},
        "runtime": {"input_schema": "text", "output_schema": "message"},
        "environment": {"provider": "local"},
        "models": {
            "default": {"provider": PROVIDER, "model": model, "base_url": gateway.url, "api_key_env": FABRIC_KEY_ENV}
        },
    }
    return json.dumps(config, indent=2)


def _fabric_launch(model: str, gateway: GatewayConfig, *, reasoning_effort: str | None = None) -> HarnessLaunch:
    """Build the launch for NeMo Fabric (:data:`FABRIC_PACKAGES`, pinned to :data:`FABRIC_VERSION`).

    The run script prints only the final response, so the bridge keeps the
    whole stdout as the answer.

    Args:
        model: Target model id.
        gateway: Gateway URL and key.
        reasoning_effort: The user's effort for the model, or ``None`` for Codex's default.

    Returns:
        The launch.
    """
    return HarnessLaunch(
        instructions_file="AGENTS.md",
        install_command=_fabric_install(),
        files={FABRIC_CONFIG_FILE: _fabric_config(model, gateway, reasoning_effort)},
        run_command=(
            # ``&&`` and not ``;``: the protected relay rewrite goes in front with
            # ``&&``, and a failed rewrite must stop the run before Fabric starts.
            f'{_FABRIC_PATH} && {FABRIC_KEY_ENV}="$APP_API_KEY" OPENAI_API_KEY= '
            f"fabric -I -c {shlex.quote(_FABRIC_RUN_SCRIPT)} {FABRIC_CONFIG_FILE}"
        ),
        env=_base_env(model, gateway),
        parse_output=parse_plain_output,
    )


SPEC = register(
    HarnessSpec(
        id=BLACKBOX_HARNESS_FABRIC,
        launch=_fabric_launch,
        binary="fabric",
        version=FABRIC_VERSION,
        output_format="plain",
        protected_env=_PROTECTED_ENV,
        position=61,
    )
)
