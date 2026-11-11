"""xAI Grok Build CLI harness (``grok``).

Adapted from Harbor 0.25.0 (Apache-2.0), agents/installed/grok_build: the
``install.sh`` release install into ``~/.grok/bin``, the generated
``config.toml`` with telemetry, codebase upload and auto-update pinned off, a
custom ``[model.<slug>]`` entry on the ``chat_completions`` backend, and the
``--single ... --always-approve --output-format streaming-json`` one-shot run.
Harbor reads Grok's session files for the answer; Skynet converts the stdout
stream to one Pi ``message_end`` event, so the bridge's Pi parser reads it.
"""

from __future__ import annotations

import json
import shlex
from urllib.parse import urlsplit

from .....exceptions import ServiceError
from .....models.blackbox import BLACKBOX_HARNESS_GROK_BUILD
from ..harness_bridge import parse_pi_output
from . import HarnessSpec, register
from .base import _PROMPT_ARG, ENV_API_KEY, ENV_MODEL, GatewayConfig, HarnessLaunch, _base_env, _version_check

# The ``stable`` release that ``https://x.ai/cli/install.sh`` served on 2026-11-08.
GROK_VERSION = "1.0.77"
GROK_BINARY = "grok"
_INSTALL_SCRIPT_URL = "https://x.ai/cli/install.sh"
_PATH_EXPORT = 'export PATH="$HOME/.grok/bin:$PATH"'
# The gateway URL lives in this file rather than on the command line, so a
# protected run's relay rewrite of generated files reaches it.
GROK_CONFIG_FILE = ".app/grok/config.toml"
# Grok accepts every effort level Skynet offers; anything else gets no flag.
_GROK_EFFORTS = frozenset({"none", "minimal", "low", "medium", "high", "xhigh", "max"})
# Grok falls back to these keys for its built-in xAI and other providers. Empty
# values override a leaked host key, so no route around the gateway exists.
_NO_DIRECT_KEYS = {"XAI_API_KEY": "", "OPENAI_API_KEY": "", "ANTHROPIC_API_KEY": ""}
_PROTECTED_ENV = {**_NO_DIRECT_KEYS, "GROK_DISABLE_AUTOUPDATER": "1", "DO_NOT_TRACK": "1"}
# The relay rewrite replaces the raw URL text in the config, so the URL must
# need no TOML escaping.
_URL_FORBIDDEN = frozenset("\"'\\")

# Grok's streaming-json reports ``reasoning_tokens`` as a part of
# ``output_tokens`` and cache reads and writes beside ``input_tokens``, so the
# prompt total adds the cache buckets and the completion total is the output.
# The converter runs inline: every generated file must carry the gateway URL
# for the relay rewrite, and this script has none.
_CONVERTER_SCRIPT = '''"""Turn Grok Build streaming-json on stdin into one Pi ``message_end`` line."""

import json
import sys


def main():
    """Join Grok's text chunks and sum its usage, then print the Pi event."""
    chunks, errors = [], []
    prompt = completion = 0
    for line in sys.stdin:
        try:
            event = json.loads(line)
        except ValueError:
            continue
        if not isinstance(event, dict):
            continue
        kind = event.get("type")
        if kind == "text" and isinstance(event.get("data"), str):
            chunks.append(event["data"])
        elif kind == "end":
            used = event.get("usage") or {}
            prompt += sum(
                int(used.get(name) or 0)
                for name in ("input_tokens", "cache_read_input_tokens", "cache_creation_input_tokens")
            )
            completion += int(used.get("output_tokens") or 0)
        elif kind == "error":
            errors.append(str(event.get("message") or "Grok Build reported an error."))
    text = "".join(chunks).strip()
    if errors and not text:
        sys.stderr.write("\\n".join(errors) + "\\n")
        return 1
    message = {
        "role": "assistant",
        "content": [{"type": "text", "text": text}],
        "usage": {"input": prompt, "output": completion},
    }
    print(json.dumps({"type": "message_end", "message": message}))
    return 0


sys.exit(main())
'''


def _gateway_url(gateway: GatewayConfig) -> str:
    """Return the gateway base URL Grok must call, or refuse the launch.

    Args:
        gateway: Gateway URL and key.

    Returns:
        The validated gateway URL.

    Raises:
        ServiceError: When the URL is not absolute http(s), embeds credentials,
            or has characters that cannot sit raw in a TOML string.
    """
    url = gateway.url
    if not url or any(char.isspace() or char in _URL_FORBIDDEN for char in url):
        raise ServiceError("The Grok Build harness needs an absolute http(s) gateway URL.")
    parts = urlsplit(url)
    if parts.scheme not in ("http", "https") or not parts.hostname or parts.fragment:
        raise ServiceError("The Grok Build harness needs an absolute http(s) gateway URL.")
    if parts.username or parts.password:
        raise ServiceError("The Grok Build gateway URL must not embed credentials.")
    return url


def _config_toml(model: str, gateway_url: str) -> str:
    """Return the Grok ``config.toml`` that routes every model role to the gateway.

    JSON string literals are valid TOML basic strings, so ``json.dumps``
    quotes the model id both as a table key and as a value.

    Args:
        model: Target model id as the gateway knows it.
        gateway_url: Validated gateway base URL.

    Returns:
        The TOML text.
    """
    slug = json.dumps(model)
    return (
        "[cli]\nauto_update = false\n\n"
        "[features]\ntelemetry = false\n\n"
        "[harness]\ndisable_codebase_upload = true\n\n"
        # Auxiliary roles default to xAI's hosted models; pinning them to the
        # same entry keeps every call on the gateway.
        f"[models]\ndefault = {slug}\nsession_summary = {slug}\n"
        f"image_description = {slug}\nweb_search = {slug}\n\n"
        f"[model.{slug}]\nname = {slug}\nmodel = {slug}\n"
        f"base_url = {json.dumps(gateway_url)}\nenv_key = {json.dumps(ENV_API_KEY)}\n"
        'api_backend = "chat_completions"\n'
    )


def _grok_install() -> str:
    """Return the command that installs the pinned Grok release unless it is already on PATH.

    Returns:
        The shell command.
    """
    return (
        f"{_PATH_EXPORT}; {_version_check(GROK_BINARY, GROK_VERSION)} || "
        f"curl -fsSL {_INSTALL_SCRIPT_URL} | bash -s {GROK_VERSION}"
    )


def _grok_launch(model: str, gateway: GatewayConfig, *, reasoning_effort: str | None = None) -> HarnessLaunch:
    """Build the launch for the xAI Grok Build CLI (``grok``, pinned to :data:`GROK_VERSION`).

    Each run gets a fresh Grok home outside the workspace, so Grok's session
    and log files never land in the files the run hands back.

    Args:
        model: Target model id as the gateway knows it.
        gateway: Gateway URL and key; Grok sends every model call there.
        reasoning_effort: The user's effort for the model, or ``None`` for Grok's default.

    Returns:
        The launch.
    """
    effort = (reasoning_effort or "").lower()
    effort_flag = f" --reasoning-effort {effort}" if effort in _GROK_EFFORTS else ""
    run_command = (
        f"{_PATH_EXPORT} && "
        'GROK_HOME_DIR="$(mktemp -d)" && mkdir -p "$GROK_HOME_DIR/.grok" && '
        f'cp {GROK_CONFIG_FILE} "$GROK_HOME_DIR/.grok/config.toml" && '
        'HOME="$GROK_HOME_DIR" GROK_LOG_FILE="$GROK_HOME_DIR/grok.log" '
        f"{GROK_BINARY} --no-auto-update --single {_PROMPT_ARG} --always-approve "
        f'--output-format streaming-json --model "${ENV_MODEL}"{effort_flag} < /dev/null '
        f"| python3 -c {shlex.quote(_CONVERTER_SCRIPT)}"
    )
    env = _base_env(model, gateway)
    env.update(_NO_DIRECT_KEYS)
    env.update({"GROK_DISABLE_AUTOUPDATER": "1", "RUST_LOG": "warn"})
    return HarnessLaunch(
        instructions_file="AGENTS.md",
        install_command=_grok_install(),
        files={GROK_CONFIG_FILE: _config_toml(model, _gateway_url(gateway))},
        run_command=run_command,
        env=env,
        parse_output=parse_pi_output,
    )


SPEC = register(
    HarnessSpec(
        id=BLACKBOX_HARNESS_GROK_BUILD,
        launch=_grok_launch,
        binary=GROK_BINARY,
        version=GROK_VERSION,
        output_format="pi",
        protected_env=_PROTECTED_ENV,
        position=67,
    )
)
