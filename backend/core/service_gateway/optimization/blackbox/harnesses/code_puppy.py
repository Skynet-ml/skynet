"""Code Puppy harness (``code-puppy``).

Adapted from Harbor 0.25.0 (Apache-2.0), agents/installed/code_puppy.py: the
install through ``uv tool install``, the seeded ``puppy.cfg`` that skips the
first-run prompts, the ``extra_models.json`` custom OpenAI endpoint that reads
its key from ``CODE_PUPPY_API_KEY``, the ``--prompt`` headless run, the guard
for a prompt that starts with ``!`` or ``/``, and the usage-file check that
turns a swallowed model error into a failed run. Skynet pins the release and
always routes the custom endpoint to the model gateway, and reads the gateway
token from an environment name that each run picks at random (see
:data:`_CUSTOM_API_KEY_ENV`).
Harbor passes ``--yolo true``, but the pinned CLI has no such argument and
exits on it, so ``puppy.cfg`` sets ``yolo_mode = true`` instead.
"""

from __future__ import annotations

import json
import shlex

from .....exceptions import ServiceError
from .....models.blackbox import BLACKBOX_HARNESS_CODE_PUPPY
from ..harness_bridge import parse_pi_output
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
# Code Puppy prints the prompt, its tool traces and status lines on stdout, so
# its stdout goes to stderr and this script prints the answer as one Pi
# ``message_end`` line. The answer is the text of the last model response in
# the session that 0.0.967 always autosaves under
# ``$XDG_CACHE_HOME/code_puppy/autosaves``; the usage file gives the tokens and
# one Pi ``tool_execution_start`` line per tool call, so the bridge's cost and
# tool-call stops read them. The answer line comes first, because a stop
# signals the whole group while the script prints. A script in ``files`` would
# land in the working directory that the harness can edit and would go through
# a protected run's relay rewrite, so it runs inline (``python3 -I -c '...'``)
# and must hold no single quote.
CODE_PUPPY_TO_PI = """import glob
import json
import os
import sys


def load(path):
    try:
        with open(path, encoding="utf-8") as handle:
            return json.load(handle)
    except (OSError, ValueError):
        return None


def count(value):
    return value if type(value) is int and value >= 0 else 0


used = load(sys.argv[1])
used = used if isinstance(used, dict) else {}
text = ""
sessions = [
    path
    for path in glob.glob(os.path.join(sys.argv[2], "*.json"))
    if not path.endswith(("_meta.json", "_acp.json"))
]
for path in sorted(sessions, key=os.path.getmtime):
    envelope = load(path)
    messages = envelope.get("messages") if isinstance(envelope, dict) else None
    for message in messages if isinstance(messages, list) else []:
        if not isinstance(message, dict) or message.get("kind") != "response":
            continue
        parts = [
            part.get("content")
            for part in message.get("parts") or []
            if isinstance(part, dict) and part.get("part_kind") == "text" and isinstance(part.get("content"), str)
        ]
        if any(parts):
            text = "\\n\\n".join(part for part in parts if part).strip()
usage = {"input": count(used.get("input_tokens")), "output": count(used.get("output_tokens"))}
content = [{"type": "text", "text": text}] if text else []
lines = [json.dumps({"type": "message_end", "message": {"role": "assistant", "content": content, "usage": usage}})]
calls = min(count(used.get("tool_calls")), 10000)
lines += [json.dumps({"type": "tool_execution_start", "toolCallId": f"code-puppy-{index}"}) for index in range(calls)]
sys.stdout.write("\\n".join(lines) + "\\n")
"""
# Code Puppy resolves a ``$NAME`` api_key of a custom endpoint from the
# environment. At startup 0.0.967 copies every model's ``$NAME`` api_key from
# ``./.env`` into the environment over the inherited value, so a candidate
# repo's ``.env`` could replace a fixed name. The workspace model file holds
# this placeholder; each run exports the token under a random name and rewrites
# the placeholder to it in the XDG copy. The token never goes into a file, and
# a repo ``.env`` cannot know the name.
_CUSTOM_API_KEY_ENV = "CODE_PUPPY_API_KEY"
# The pipeline's status is ``tr``'s, so a missing ``od`` or ``/dev/urandom``
# would leave the fixed name ``CP_KEY_`` that a repo ``.env`` can know. The
# length check (7 + 16 hex digits) fails the run instead.
_RANDOM_KEY_ENV = (
    f"_cp_key=\"CP_KEY_$(od -An -N8 -tx1 /dev/urandom | tr -d ' \\n')\" && "
    '{ [ "${#_cp_key}" -eq 23 ] || '
    "{ echo 'code-puppy: could not make a random key name (od or /dev/urandom is missing)' >&2; exit 1; }; } && "
    f'export "$_cp_key=${ENV_API_KEY}"'
)
# Code Puppy's effort names (model_utils._OPENAI_REASONING_EFFORT_ORDER in
# 0.0.967). Each passes unchanged; the launch refuses any other effort, so a run
# never gets an effort the user did not pick.
CODE_PUPPY_EFFORTS = ("none", "minimal", "low", "medium", "high", "xhigh", "max")
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
# frames those prompts and leaves every other prompt byte-identical. Code Puppy
# finds the leading character after Python's ``str.strip()``, which also strips
# Unicode whitespace such as a non-breaking space, so the check runs in Python
# too. It exits 0 only for a plain prompt, so a failed check frames the prompt.
_PLAIN_PROMPT_CHECK = (
    "import sys; "
    'text = open(sys.argv[1], encoding="utf-8", errors="replace").read().lstrip(); '
    'sys.exit(text.startswith(("!", "/")))'
)
_FRAMED_PROMPT = (
    f'_cp_prompt="$(cat "${ENV_PROMPT_FILE}")"; '
    f"python3 -I -c '{_PLAIN_PROMPT_CHECK}' \"${ENV_PROMPT_FILE}\" || "
    '_cp_prompt="Task request:\n$_cp_prompt"; '
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


def _check_effort(reasoning_effort: str) -> None:
    """Refuse an effort that Code Puppy does not know.

    Args:
        reasoning_effort: The user's effort for the model.

    Raises:
        ServiceError: When the effort is not in :data:`CODE_PUPPY_EFFORTS`.
    """
    if reasoning_effort not in CODE_PUPPY_EFFORTS:
        raise ServiceError(
            f"The Code Puppy harness does not support the effort '{reasoning_effort}'. "
            f"Pick one of: {', '.join(CODE_PUPPY_EFFORTS)}."
        )


def _puppy_cfg(reasoning_effort: str | None) -> str:
    """Build ``puppy.cfg`` for an unattended run.

    Args:
        reasoning_effort: The user's effort for the model, or ``None`` for Code Puppy's default.

    Returns:
        The INI text.

    Raises:
        ServiceError: When Code Puppy has no effort with that name.
    """
    lines = [
        "[puppy]",
        # Code Puppy prompts on stdin for these two when they are missing.
        "puppy_name = Puppy",
        "owner_name = User",
        "auto_save_session = false",
        # 0.0.967 has no --yolo argument (Harbor 0.25.0 passes one); this key skips shell-command approval.
        "yolo_mode = true",
    ]
    if reasoning_effort:
        _check_effort(reasoning_effort)
        lines.append(f"model_settings_{PROVIDER}_reasoning_effort = {reasoning_effort}")
    return "\n".join(lines) + "\n"


def _extra_models(model: str, gateway: GatewayConfig, reasoning_effort: str | None) -> str:
    """Build ``extra_models.json`` with one gateway-backed model under the alias :data:`PROVIDER`.

    Args:
        model: Target model id as the gateway knows it.
        gateway: Gateway URL and key; only the URL is written.
        reasoning_effort: The user's effort for the model, or ``None`` for Code Puppy's default.

    Returns:
        The JSON text.

    Raises:
        ServiceError: When Code Puppy has no effort with that name.
    """
    entry: dict[str, object] = {
        "type": "custom_openai",
        "name": model,
        "custom_endpoint": {"url": gateway.url, "api_key": f"${_CUSTOM_API_KEY_ENV}"},
    }
    if reasoning_effort:
        _check_effort(reasoning_effort)
        # 0.0.967 drops the puppy.cfg effort for a custom_openai model unless the
        # model advertises it (config.model_supports_setting) or its name looks
        # like a GPT-5 or o-series id, so a gateway id such as a Claude model
        # would silently lose it. Advertising the one chosen effort makes
        # make_model_settings send it as ``reasoning_effort``.
        entry["setting_choices"] = {"reasoning_effort": [reasoning_effort]}
    return json.dumps({PROVIDER: entry}, indent=2) + "\n"


def _code_puppy_launch(model: str, gateway: GatewayConfig, *, reasoning_effort: str | None = None) -> HarnessLaunch:
    """Build the launch for Code Puppy (:data:`CODE_PUPPY_PACKAGE`, pinned to :data:`CODE_PUPPY_VERSION`).

    Code Puppy has no JSON event stream on stdout, so its stdout goes to
    stderr and :data:`CODE_PUPPY_TO_PI` prints the answer and usage as Pi
    lines for the bridge's Pi parser. Code Puppy exits 0 after a model error
    and writes the usage file only after a real result, so the run fails when
    that file is missing.

    Args:
        model: Target model id.
        gateway: Gateway URL and key.
        reasoning_effort: The user's effort for the model, or ``None`` for Code Puppy's default.

    Returns:
        The launch.

    Raises:
        ServiceError: When Code Puppy has no effort with that name.
    """
    xdg_exports = " ".join(f'{name}="$_cp_dir/{folder}"' for name, folder in _XDG_DIRS.items())
    run_command = (
        f"{_CODE_PUPPY_PATH}; "
        '_cp_dir="$(mktemp -d)" || exit 1; '
        """trap 'rm -rf "$_cp_dir"' EXIT; """
        f"export {xdg_exports}; "
        'mkdir -p "$XDG_CONFIG_HOME/code_puppy" "$XDG_DATA_HOME/code_puppy" && '
        f"{_RANDOM_KEY_ENV} && "
        f'sed "s/[$]{_CUSTOM_API_KEY_ENV}/\\$$_cp_key/" {CODE_PUPPY_MODELS_FILE} '
        '> "$XDG_DATA_HOME/code_puppy/extra_models.json" && '
        f"printf '%s' {shlex.quote(_puppy_cfg(reasoning_effort))} > \"$XDG_CONFIG_HOME/code_puppy/puppy.cfg\" || exit 1; "
        f"{_FRAMED_PROMPT}"
        "code-puppy --disable-ask-user-question "
        f'--usage-file {_USAGE_FILE} --model {PROVIDER} --prompt "$_cp_prompt" < /dev/null >&2; '
        "_cp_status=$?; "
        f"if [ ! -s {_USAGE_FILE} ]; then "
        "echo 'code-puppy produced no result: its usage file was not written' >&2; exit 1; fi; "
        f"python3 -I -c '{CODE_PUPPY_TO_PI}' {_USAGE_FILE} \"$XDG_CACHE_HOME/code_puppy/autosaves\" || exit 1; "
        "exit $_cp_status"
    )
    return HarnessLaunch(
        instructions_file="AGENTS.md",
        install_command=_code_puppy_install(),
        files={CODE_PUPPY_MODELS_FILE: _extra_models(model, gateway, reasoning_effort)},
        run_command=run_command,
        env={**_base_env(model, gateway), **_BLANK_PROVIDER_KEYS},
        parse_output=parse_pi_output,
    )


SPEC = register(
    HarnessSpec(
        id=BLACKBOX_HARNESS_CODE_PUPPY,
        launch=_code_puppy_launch,
        binary="code-puppy",
        version=CODE_PUPPY_VERSION,
        output_format="pi",
        protected_env=_PROTECTED_ENV,
        position=90,
    )
)
