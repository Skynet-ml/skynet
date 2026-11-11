"""xAI Grok Build CLI harness (``grok``).

Adapted from Harbor 0.25.0 (Apache-2.0), agents/installed/grok_build: the
release binary in ``~/.grok/bin``, the generated ``config.toml`` with
telemetry, codebase upload and auto-update pinned off, a custom
``[model.<slug>]`` entry on the ``chat_completions`` backend, the
``--single ... --always-approve --output-format streaming-json`` one-shot run,
the ``--reasoning-effort`` choices, and the answer read from the session's
``chat_history.jsonl``. Harbor pipes ``x.ai/cli/install.sh`` into bash and
gives Grok a direct provider key; Skynet downloads the pinned release binary
itself and points the custom model entry at the model gateway instead. Harbor
converts the session on the host; Skynet converts it in the sandbox to one Pi
``message_end`` event, so the bridge's Pi parser reads it.
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

# The ``stable`` release that ``https://x.ai/cli/stable`` served on 2026-11-10.
GROK_VERSION = "1.0.77"
GROK_BINARY = "grok"
# The address ``install.sh`` downloads a pinned release from, raw or with a
# ``.zst`` suffix.
_RELEASE_URL = f"https://x.ai/cli/grok-{GROK_VERSION}-linux"
# sha256 of the release binary, taken on 2026-11-10 from the downloaded ``.zst``
# files after decompression (each matches the raw file's Content-Length). xAI
# publishes no checksum, so a swapped binary fails the install instead of running.
GROK_SHA256 = {
    "x86_64": "b702950aa5827d855e493278ab6215a4e0f5bceb5bba6c6243a9f55cdd143b0a",
    "aarch64": "b30eee14a656124ef3c9da8577b5e783ef2cffabeb2b679c5c49633e77eed574",
}
_GROK_BIN = '"$HOME/.grok/bin"'
_PATH_EXPORT = f'export PATH={_GROK_BIN}:"$PATH"'
_DOWNLOADED = '"$d/grok"'
# The gateway URL lives in this file rather than on the command line, so a
# protected run's relay rewrite of generated files reaches it.
GROK_CONFIG_FILE = ".app/grok/config.toml"
# The ``--reasoning-effort`` choices of Grok Build (Harbor's
# ``_GROK_REASONING_EFFORT_CHOICES``). Skynet passes the user's level as it is.
GROK_EFFORTS = ("none", "minimal", "low", "medium", "high", "xhigh", "max")
# Each run has a fresh Grok home, so a fixed id never collides. It lets the
# converter read the main session and not a subagent's.
_SESSION_ID = "6b1f3a52-58f4-4c1e-9d1e-5c0a6f2d7b41"
# Grok falls back to these keys for its built-in xAI and other providers, and
# the installer reads the deployment key. Empty values override a leaked host
# key, so no route around the gateway exists.
_NO_DIRECT_KEYS = {"XAI_API_KEY": "", "OPENAI_API_KEY": "", "ANTHROPIC_API_KEY": "", "GROK_DEPLOYMENT_KEY": ""}
_PROTECTED_ENV = {**_NO_DIRECT_KEYS, "GROK_DISABLE_AUTOUPDATER": "1", "DO_NOT_TRACK": "1"}
# The relay rewrite replaces the raw URL text in the config, so the URL must
# need no TOML escaping.
_URL_FORBIDDEN = frozenset("\"'\\")

# Grok's streaming-json ``end`` event carries the run total (Harbor's
# ``GrokUsage``): ``reasoning_tokens`` is a part of ``output_tokens``, and cache
# reads and writes sit beside ``input_tokens``. So the prompt total adds the
# cache buckets and the completion total is the output. Without an ``end``
# event, the per-call ``usage`` events add up. Grok exits 0 on an ``end`` event
# whose ``stopReason`` is ``cancelled`` (for example when the gateway drops
# mid-run), so only an ``end`` event with ``end_turn`` gives an answer. A
# missing ``end`` event or another stop reason fails the run with status 1, and
# a non-zero Grok exit keeps Grok's status; both drop the text of earlier turns.
# The converter runs inline: a script in ``files`` would land in the workspace
# that Grok can edit.
_CONVERTER_SCRIPT = '''"""Turn a Grok Build run into one Pi ``message_end`` line."""

import glob
import json
import os
import sys

_PROMPT_BUCKETS = ("input_tokens", "cache_read_input_tokens", "cache_creation_input_tokens")


def json_lines(path):
    """Return the JSON objects in the file at ``path``, one for each line."""
    try:
        with open(path, encoding="utf-8", errors="replace") as handle:
            lines = handle.read().splitlines()
    except OSError:
        return []
    found = []
    for line in lines:
        try:
            item = json.loads(line)
        except ValueError:
            continue
        if isinstance(item, dict):
            found.append(item)
    return found


def count(used, name):
    """Return one token bucket, or 0 when it is missing or not a count."""
    value = used.get(name)
    return value if type(value) is int and value >= 0 else 0


def text_of(content):
    """Join Grok message content, a string or a list of text blocks."""
    if isinstance(content, str):
        return content
    if isinstance(content, list):
        return "".join(str(b.get("text") or "") for b in content if isinstance(b, dict))
    return ""


def main():
    """Print the run usage as a Pi event, with the last assistant text only for a finished turn."""
    stream, home, session, status = sys.argv[1:5]
    calls, total, end = [], None, None
    for event in json_lines(stream):
        if event.get("type") == "end":
            end = event
        used = event.get("usage")
        if not isinstance(used, dict):
            continue
        if event.get("type") == "usage":
            calls.append(used)
        elif event.get("type") in ("end", "error"):
            total = used
    picked = calls if total is None else [total]
    prompt = sum(count(used, name) for used in picked for name in _PROMPT_BUCKETS)
    completion = sum(count(used, "output_tokens") for used in picked)
    usage = {"input": prompt, "output": completion}
    if status != "0" or end is None or end.get("stopReason") != "end_turn":
        print(json.dumps({"type": "message_end", "message": {"role": "assistant", "content": [], "usage": usage}}))
        sys.exit(int(status) or 1)
    text = ""
    pattern = os.path.join(home, ".grok", "sessions", "**", session, "chat_history.jsonl")
    for path in sorted(glob.glob(pattern, recursive=True)):
        for message in json_lines(path):
            if message.get("type") == "assistant":
                text = text_of(message.get("content")).strip() or text
    content = [{"type": "text", "text": text}] if text else []
    message = {"role": "assistant", "content": content, "usage": usage}
    print(json.dumps({"type": "message_end", "message": message}))


main()
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


def _effort_flag(reasoning_effort: str | None) -> str:
    """Return the ``--reasoning-effort`` argument for the user's level.

    Args:
        reasoning_effort: The user's effort for the model, or ``None`` or an empty string for Grok's default.

    Returns:
        The argument with a leading space, or an empty string for no effort.

    Raises:
        ServiceError: When Grok Build does not know the level.
    """
    if not reasoning_effort:
        return ""
    if reasoning_effort not in GROK_EFFORTS:
        raise ServiceError(
            f"Grok Build does not support the reasoning effort '{reasoning_effort}'. "
            f"Use one of: {', '.join(GROK_EFFORTS)}."
        )
    return f" --reasoning-effort {reasoning_effort}"


def _config_toml(model: str, gateway_url: str, reasoning_effort: str | None) -> str:
    """Return the Grok ``config.toml`` that routes every model role to the gateway.

    JSON string literals are valid TOML basic strings, so ``json.dumps``
    quotes the model id both as a table key and as a value. The URL keeps its
    raw non-ASCII characters, so the relay rewrite finds it byte for byte.

    Args:
        model: Target model id as the gateway knows it.
        gateway_url: Validated gateway base URL.
        reasoning_effort: A level from :data:`GROK_EFFORTS`, or ``None`` or an empty string for Grok's default.

    Returns:
        The TOML text.
    """
    slug = json.dumps(model)
    url = json.dumps(gateway_url, ensure_ascii=False)
    return (
        # Web search is an xAI server-side tool that does not go through the gateway.
        "disable_web_search = true\n\n"
        "[cli]\nauto_update = false\n\n"
        "[features]\ntelemetry = false\n\n"
        "[harness]\ndisable_codebase_upload = true\n\n"
        # Grok fetches its model list from this base at start, outside the
        # model entry; pointing it at the gateway keeps that call off api.x.ai.
        f"[endpoints]\nxai_api_base_url = {url}\n\n"
        # Auxiliary roles default to xAI's hosted models; pinning them to the
        # same entry keeps every call on the gateway.
        f"[models]\ndefault = {slug}\nsession_summary = {slug}\n"
        f"image_description = {slug}\nweb_search = {slug}\n\n"
        f"[model.{slug}]\nname = {slug}\nmodel = {slug}\n"
        f"base_url = {url}\nenv_key = {json.dumps(ENV_API_KEY)}\n"
        'api_backend = "chat_completions"\n'
        # Grok 1.0.77 drops ``--reasoning-effort`` with only a warning for a
        # custom model whose menu lacks the level, and the gateway's model list
        # advertises none. Declaring the level keeps it on the wire.
        + (
            f"\n[[model.{slug}.reasoning_efforts]]\nvalue = {json.dumps(reasoning_effort)}\n"
            if reasoning_effort
            else ""
        )
    )


def _grok_install() -> str:
    """Return the command that installs the pinned Grok release unless it is already on PATH.

    Returns:
        The shell command. It downloads the release binary into a fresh
        folder, checks its sha256 digest and then the version it reports, and
        moves it into ``$HOME/.grok/bin``. It removes the download folder on
        success and on failure, and exits non-zero when a step fails.
    """
    arch_cases = " ".join(
        f"{pattern}) a={arch}; s={GROK_SHA256[arch]};;"
        for pattern, arch in (("x86_64|amd64", "x86_64"), ("aarch64|arm64", "aarch64"))
    )
    # The bootstrap runs this inside its own ``set -eu`` script, so the step
    # records its status with ``|| r=$?`` (exempt from ``set -e``) and removes
    # the download folder before it fails, rather than with an EXIT trap that
    # would replace the caller's.
    return (
        f"{_PATH_EXPORT}; {_version_check(GROK_BINARY, GROK_VERSION)} || {{ "
        f'case "$(uname -m)" in {arch_cases} *) echo "grok: unsupported architecture" >&2; exit 1;; esac; '
        'd="$(mktemp -d)"; r=0; '
        # The ``.zst`` file is about a third of the raw size. The runtime image
        # has no zstd, so the raw file is the fallback, as in xAI's install.sh.
        # Both paths check the digest of the final binary before it runs.
        "{ if command -v zstd >/dev/null 2>&1; then "
        f'curl -fsSL -o "$d/grok.zst" "{_RELEASE_URL}-$a.zst" && zstd -q -d -o "$d/grok" "$d/grok.zst"; '
        f'else curl -fsSL -o "$d/grok" "{_RELEASE_URL}-$a"; fi; }} && '
        'echo "$s  $d/grok" | sha256sum -c - >/dev/null && chmod 755 "$d/grok" && '
        f"{_version_check(_DOWNLOADED, GROK_VERSION)} && "
        f'mkdir -p {_GROK_BIN} && mv "$d/grok" {_GROK_BIN}/grok || r=$?; '
        'rm -rf "$d"; [ "$r" -eq 0 ] || exit "$r"; }'
    )


def _grok_launch(model: str, gateway: GatewayConfig, *, reasoning_effort: str | None = None) -> HarnessLaunch:
    """Build the launch for the xAI Grok Build CLI (``grok``, pinned to :data:`GROK_VERSION`).

    Each run gets a fresh Grok home outside the workspace, so Grok's session
    and log files never land in the files the run hands back. An EXIT trap
    removes it. The run keeps Grok's exit status.

    Args:
        model: Target model id as the gateway knows it.
        gateway: Gateway URL and key; Grok sends every model call there.
        reasoning_effort: The user's effort for the model, or ``None`` or an empty string for Grok's default.

    Returns:
        The launch.

    Raises:
        ServiceError: When the gateway URL is not usable or Grok does not know the effort.
    """
    effort_flag = _effort_flag(reasoning_effort)
    run_command = (
        f"{_PATH_EXPORT} && "
        'g="$(mktemp -d)" && trap \'rm -rf "$g"\' EXIT && mkdir -p "$g/.grok" && '
        f'cp {GROK_CONFIG_FILE} "$g/.grok/config.toml" && {{ '
        'HOME="$g" GROK_LOG_FILE="$g/grok.log" '
        # ``--single=`` keeps a prompt that starts with "-" a value; clap reads a
        # separate argument that starts with "-" as a flag.
        f"{GROK_BINARY} --no-auto-update --single={_PROMPT_ARG} --always-approve "
        f'--output-format streaming-json --session-id {_SESSION_ID} --model "${ENV_MODEL}"{effort_flag} '
        '< /dev/null > "$g/stream.jsonl"; s=$?; '
        f'python3 -I -c {shlex.quote(_CONVERTER_SCRIPT)} "$g/stream.jsonl" "$g" {_SESSION_ID} "$s"; }}'
    )
    env = _base_env(model, gateway)
    env.update(_NO_DIRECT_KEYS)
    env.update({"GROK_DISABLE_AUTOUPDATER": "1", "DO_NOT_TRACK": "1", "RUST_LOG": "warn"})
    return HarnessLaunch(
        instructions_file="AGENTS.md",
        install_command=_grok_install(),
        files={GROK_CONFIG_FILE: _config_toml(model, _gateway_url(gateway), reasoning_effort)},
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
        supported_efforts=GROK_EFFORTS,
        position=67,
    )
)
