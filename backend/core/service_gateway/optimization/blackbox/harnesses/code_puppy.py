"""Code Puppy harness (``code-puppy``).

Adapted from Harbor 0.25.0 (Apache-2.0), agents/installed/code_puppy.py: the
install through ``uv tool install``, the seeded ``puppy.cfg`` that skips the
first-run prompts, the ``extra_models.json`` custom OpenAI endpoint that reads
its key from ``CODE_PUPPY_API_KEY``, the ``--yolo true --prompt`` headless run, the guard
for a prompt that starts with ``!`` or ``/``, and the usage-file check that
turns a swallowed model error into a failed run. Skynet pins the release and
always routes the custom endpoint to the model gateway.
"""

from __future__ import annotations

import json
import shlex

from .....models.blackbox import BLACKBOX_HARNESS_CODE_PUPPY
from ..harness_bridge import parse_plain_output
from . import HarnessSpec, register
from .base import ENV_API_KEY, ENV_PROMPT_FILE, PROVIDER, GatewayConfig, HarnessLaunch, _base_env, _version_check

CODE_PUPPY_PACKAGE = "code-puppy"
# PyPI ``info.version`` for ``code-puppy`` on 2026-11-08 (MIT).
CODE_PUPPY_VERSION = "0.0.967"
# The pinned uv release installs the pinned package; Code Puppy needs Python < 3.15.
CODE_PUPPY_UV_VERSION = "0.13.7"
CODE_PUPPY_PYTHON = "3.13"
# A protected run's relay rewrite expects the gateway URL in every launch file,
# so the model file ships in the workspace (the bridge keeps launch files out of
# the version patch). It holds no key.
CODE_PUPPY_MODELS_FILE = ".app/code_puppy/extra_models.json"
# Code Puppy reads ``$XDG_CONFIG_HOME/code_puppy/puppy.cfg`` and
# ``$XDG_DATA_HOME/code_puppy/extra_models.json``, and writes its sessions and
# caches under the XDG dirs. A file it writes in the workspace joins the version
# patch, which the parent scores and can open as a pull request, so the XDG
# dirs and the usage file live in a temporary folder outside the workspace.
_XDG_DIRS = {
    "XDG_CONFIG_HOME": "config",
    "XDG_DATA_HOME": "data",
    "XDG_CACHE_HOME": "cache",
    "XDG_STATE_HOME": "state",
}
_USAGE_FILE = '"$_cp_dir/usage.json"'
# Code Puppy resolves a ``$NAME`` api_key of a custom endpoint from this variable.
_CUSTOM_API_KEY_ENV = "CODE_PUPPY_API_KEY"
# Code Puppy's effort names, written to puppy.cfg; anything else gets no setting.
_CODE_PUPPY_EFFORTS = frozenset({"minimal", "low", "medium", "high", "xhigh", "max"})
# ``uv tool install`` and the uv installer both put their binaries here.
_CODE_PUPPY_PATH = 'export PATH="$HOME/.local/bin:$PATH"'
# Code Puppy reads these keys for its native providers. Every launch blanks
# them, so an inherited key never gives it a route around the gateway.
_BLANK_PROVIDER_KEYS = {"OPENAI_API_KEY": "", "ANTHROPIC_API_KEY": "", "GEMINI_API_KEY": ""}
_PROTECTED_ENV = {
    # Code Puppy skips its startup version check when this is set.
    "NO_VERSION_UPDATE": "1",
    # Code Puppy documents no telemetry switch; this is the generic opt-out.
    "DO_NOT_TRACK": "1",
}
# Code Puppy treats a leading ``!`` as a shell pass-through and a leading
# ``/`` as a slash command, so such a prompt never reaches the model. Harbor
# frames those prompts and leaves every other prompt byte-identical.
_FRAMED_PROMPT = (
    f'_cp_prompt="$(cat "${ENV_PROMPT_FILE}")"; '
    'case "${_cp_prompt#"${_cp_prompt%%[![:space:]]*}"}" in '
    "'!'*|/*) _cp_prompt=\"Task request:\n$_cp_prompt\" ;; esac; "
)


def _code_puppy_install() -> str:
    """Return the command that pins Code Puppy to :data:`CODE_PUPPY_VERSION` and logs the version that will run.

    Harbor runs the latest uv installer and takes the latest release unless
    told otherwise; this pins uv and the package instead.

    Returns:
        The shell command.
    """
    return (
        f"{_CODE_PUPPY_PATH}; {_version_check('code-puppy', CODE_PUPPY_VERSION)} || "
        "{ { command -v uv >/dev/null 2>&1 || "
        f"curl -LsSf https://astral.sh/uv/{CODE_PUPPY_UV_VERSION}/install.sh | sh; }} && "
        f"uv tool install --force --python {CODE_PUPPY_PYTHON} "
        f"{CODE_PUPPY_PACKAGE}=={CODE_PUPPY_VERSION} >/dev/null; }} "
        "&& code-puppy --version"
    )


def _puppy_cfg(reasoning_effort: str | None) -> str:
    """Build ``puppy.cfg`` for an unattended run.

    Args:
        reasoning_effort: The user's effort for the model, or ``None`` for Code Puppy's default.

    Returns:
        The INI text.
    """
    lines = [
        "[puppy]",
        # Code Puppy prompts on stdin for these two when they are missing.
        "puppy_name = Puppy",
        "owner_name = User",
        "auto_save_session = false",
    ]
    if reasoning_effort in _CODE_PUPPY_EFFORTS:
        lines.append(f"model_settings_{PROVIDER}_reasoning_effort = {reasoning_effort}")
    return "\n".join(lines) + "\n"


def _extra_models(model: str, gateway: GatewayConfig) -> str:
    """Build ``extra_models.json`` with one gateway-backed model under the alias :data:`PROVIDER`.

    Args:
        model: Target model id as the gateway knows it.
        gateway: Gateway URL and key; only the URL is written.

    Returns:
        The JSON text.
    """
    entry = {
        "type": "custom_openai",
        "name": model,
        "custom_endpoint": {"url": gateway.url, "api_key": f"${_CUSTOM_API_KEY_ENV}"},
    }
    return json.dumps({PROVIDER: entry}, indent=2) + "\n"


def _code_puppy_launch(model: str, gateway: GatewayConfig, *, reasoning_effort: str | None = None) -> HarnessLaunch:
    """Build the launch for Code Puppy (:data:`CODE_PUPPY_PACKAGE`, pinned to :data:`CODE_PUPPY_VERSION`).

    Code Puppy has no JSON event stream on stdout, so the bridge keeps its
    whole stdout as the answer. Code Puppy exits 0 after a model error and
    writes the usage file only after a real result, so the run fails when
    that file is missing.

    Args:
        model: Target model id.
        gateway: Gateway URL and key.
        reasoning_effort: The user's effort for the model, or ``None`` for Code Puppy's default.

    Returns:
        The launch.
    """
    xdg_exports = " ".join(f'{name}="$_cp_dir/{folder}"' for name, folder in _XDG_DIRS.items())
    run_command = (
        f"{_CODE_PUPPY_PATH}; "
        '_cp_dir="$(mktemp -d)" || exit 1; '
        """trap 'rm -rf "$_cp_dir"' EXIT; """
        f"export {xdg_exports}; "
        'mkdir -p "$XDG_CONFIG_HOME/code_puppy" "$XDG_DATA_HOME/code_puppy" && '
        f'cp {CODE_PUPPY_MODELS_FILE} "$XDG_DATA_HOME/code_puppy/extra_models.json" && '
        f"printf '%s' {shlex.quote(_puppy_cfg(reasoning_effort))} > \"$XDG_CONFIG_HOME/code_puppy/puppy.cfg\" || exit 1; "
        f"{_FRAMED_PROMPT}"
        f'{_CUSTOM_API_KEY_ENV}="${ENV_API_KEY}" code-puppy --yolo true --disable-ask-user-question '
        f'--usage-file {_USAGE_FILE} --model {PROVIDER} --prompt "$_cp_prompt" < /dev/null; '
        "_cp_status=$?; "
        f"if [ ! -s {_USAGE_FILE} ]; then "
        "echo 'code-puppy produced no result: its usage file was not written' >&2; exit 1; fi; "
        "exit $_cp_status"
    )
    return HarnessLaunch(
        instructions_file="AGENTS.md",
        install_command=_code_puppy_install(),
        files={CODE_PUPPY_MODELS_FILE: _extra_models(model, gateway)},
        run_command=run_command,
        env={**_base_env(model, gateway), **_BLANK_PROVIDER_KEYS},
        parse_output=parse_plain_output,
    )


SPEC = register(
    HarnessSpec(
        id=BLACKBOX_HARNESS_CODE_PUPPY,
        launch=_code_puppy_launch,
        binary="code-puppy",
        version=CODE_PUPPY_VERSION,
        output_format="plain",
        protected_env=_PROTECTED_ENV,
        position=90,
    )
)
