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
_UV_RELEASE_URL = f"https://github.com/astral-sh/uv/releases/download/{FABRIC_UV_VERSION}"
# sha256 digests the GitHub release API reports for the uv musl tarballs, so a
# swapped asset fails the install instead of running.
FABRIC_UV_SHA256 = {
    "x86_64": "287491da03f2a785c70fa75b13e1f849918c2f2b5415fe84876b8214eeb792f9",
    "aarch64": "2d3ec076a0c4c0e8250bb6418c44080828b544f1a3be0b6e46adf9fb6923de45",
}
FABRIC_PYTHON = "3.12"
FABRIC_CONFIG_FILE = ".app/fabric/config.json"
# The Codex adapter reads a custom provider's key from the variable that
# ``api_key_env`` names and passes that one variable through to Codex. The
# runner path puts no rule on the name, so it reads the gateway token directly.
FABRIC_KEY_ENV = "APP_API_KEY"
# The uv installer puts uv here, and the install puts the ``fabric`` launcher here.
_FABRIC_PATH = 'export PATH="$HOME/.local/bin:$PATH"'
# The install calls uv by this path, never through ``PATH``, so an unpinned uv
# that the box already has never builds the venv.
_FABRIC_UV = '"$HOME/.local/bin/uv"'
# Fabric has no CLI of its own (``fabric-agent-ctl`` has no ``--version``), so
# the install writes this launcher. It answers the pinned version probe with the
# installed runtime release and otherwise runs the venv's Python. The probe
# fails when the Codex adapter release differs from the runtime's, so a venv
# with a mismatched adapter fails the pin check. It finds the venv next to
# itself, so a later run with another ``$HOME`` still finds it.
_FABRIC_LAUNCHER = (
    "#!/bin/sh\n"
    'venv="$(cd "$(dirname "$0")/../share/app-fabric" && pwd)" || exit 127\n'
    'if [ "$1" = "--version" ]; then\n'
    '  exec "$venv/bin/python" -I -c '
    "'import importlib.metadata as m, sys; "
    'r = m.version("nemo-fabric-runtime"); a = m.version("nemo-fabric-adapters-codex"); '
    'sys.exit("fabric: adapters-codex " + a + " differs from runtime " + r) if a != r else print("fabric", r)\'\n'
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
# The adapter merges these dotted keys into the Codex thread config, the same
# layer as ``codex -c``. The gateway prices only function and custom tools, so
# web search and multi-agent tools stay off, as in the Codex harness (SKY-529).
# The retry and websocket settings also match it: a refused request fails at
# once, and Codex talks to the gateway over plain HTTP.
_FABRIC_CODEX_OVERRIDES = {
    "analytics.enabled": False,
    "check_for_update_on_startup": False,
    "web_search": "disabled",
    "features.multi_agent": False,
    f"model_providers.{PROVIDER}.request_max_retries": 0,
    f"model_providers.{PROVIDER}.stream_max_retries": 0,
    f"model_providers.{PROVIDER}.supports_websockets": False,
}
_PROTECTED_ENV = {
    "OPENAI_API_KEY": "",
    "OTEL_SDK_DISABLED": "true",
    "DO_NOT_TRACK": "1",
}
# Runs inside the box with the venv's Python and only the standard library. It
# goes inline on the command line, not as a launch file: the protected relay
# rewrite needs every launch file to carry the gateway URL.
# Fabric's state, Codex's home (the adapter puts it under the artifacts
# folder) and the logs go to a fresh directory outside the workspace, so they
# never show up in the scored workspace. The script removes it on exit: one box
# runs many proposer turns. A SIGKILL at the turn timeout skips that cleanup, so
# each run first removes the folders of dead runs. A live run holds an flock on
# its ``owner.lock``, and the kernel drops the lock when the run dies, so a
# concurrent run's folder stays. The lock file gets its final name only after
# it is locked, so a sweep never sees an unlocked lock of a live run.
_FABRIC_RUN_SCRIPT = """\
import fcntl
import json
import os
import shutil
import subprocess
import sys
import tempfile
from pathlib import Path


def sweep(root):
    for old in root.glob("app-fabric-*"):
        if old.is_symlink():
            continue
        try:
            fd = os.open(old / "owner.lock", os.O_RDONLY)
        except OSError:
            continue
        try:
            fcntl.flock(fd, fcntl.LOCK_EX | fcntl.LOCK_NB)
        except OSError:
            continue
        else:
            shutil.rmtree(old, ignore_errors=True)
        finally:
            os.close(fd)


def main():
    config = json.loads(Path(sys.argv[1]).read_text(encoding="utf-8"))
    prompt = Path(os.environ["APP_PROMPT_FILE"]).read_text(encoding="utf-8")
    sweep(Path(tempfile.gettempdir()))
    state = Path(tempfile.mkdtemp(prefix="app-fabric-"))
    lock = os.open(state / "owner.tmp", os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
    fcntl.flock(lock, fcntl.LOCK_EX)
    os.rename(state / "owner.tmp", state / "owner.lock")
    try:
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
    finally:
        shutil.rmtree(state, ignore_errors=True)


sys.exit(main())
"""


def _uv_install() -> str:
    """Return a command that installs the pinned uv release into ``$HOME/.local/bin``.

    Returns:
        The shell command. It downloads one tarball and checks its sha256
        digest before it installs the binary. It removes the download folder
        on success and on failure, and exits non-zero when a step fails.
    """
    arch_cases = " ".join(
        f"{pattern}) a={arch}; s={FABRIC_UV_SHA256[arch]};;"
        for pattern, arch in (("x86_64|amd64", "x86_64"), ("aarch64|arm64", "aarch64"))
    )
    # Same shape as the goose install: ``|| r=$?`` keeps the bootstrap's
    # ``set -e`` from skipping the cleanup, and no EXIT trap replaces the caller's.
    return (
        f'case "$(uname -m)" in {arch_cases} *) echo "fabric: unsupported architecture" >&2; exit 1;; esac; '
        'f="uv-$a-unknown-linux-musl.tar.gz"; d="$(mktemp -d)"; r=0; '
        f'curl -fsSL -o "$d/$f" "{_UV_RELEASE_URL}/$f" && '
        'echo "$s  $d/$f" | sha256sum -c - >/dev/null && '
        'tar -xzf "$d/$f" -C "$d" && '
        'mkdir -p "$HOME/.local/bin" && '
        'install -m 755 "$d/uv-$a-unknown-linux-musl/uv" "$HOME/.local/bin/uv" || r=$?; '
        'rm -rf "$d"; [ "$r" -eq 0 ] || exit "$r"'
    )


def _fabric_install() -> str:
    """Return the command that pins Fabric to :data:`FABRIC_VERSION` and logs the version that will run.

    Harbor installs an unpinned ``fabric_package`` into a task-local venv; this
    pins uv (checked against its sha256 digest), Python, the runtime and the
    Codex adapter instead, and writes the launcher inline so it does not depend
    on when the launch files arrive.

    Returns:
        The shell command.
    """
    venv = '"$HOME/.local/share/app-fabric"'
    venv_python = '"$HOME/.local/share/app-fabric/bin/python"'
    packages = " ".join(shlex.quote(package) for package in FABRIC_PACKAGES)
    # A release build prints ``uv 0.13.7 (<commit> <date>)``; any other answer
    # (no uv, or another release) downloads the pinned one over it.
    pinned_uv = f'"uv {FABRIC_UV_VERSION}"|"uv {FABRIC_UV_VERSION} "*'
    return (
        f"{_FABRIC_PATH}; {_version_check('fabric', FABRIC_VERSION)} || "
        f'{{ {{ case "$({_FABRIC_UV} --version 2>/dev/null)" in {pinned_uv}) ;; *) {_uv_install()};; esac; }} && '
        f"rm -rf {venv} && {_FABRIC_UV} venv --python {FABRIC_PYTHON} {venv} >/dev/null && "
        f"{_FABRIC_UV} pip install --python {venv_python} {packages} >/dev/null && "
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
        "config_overrides": _FABRIC_CODEX_OVERRIDES,
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
            f"{_FABRIC_PATH} && OPENAI_API_KEY= fabric -I -c {shlex.quote(_FABRIC_RUN_SCRIPT)} {FABRIC_CONFIG_FILE}"
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
