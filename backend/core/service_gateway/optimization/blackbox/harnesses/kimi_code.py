"""Kimi Code CLI harness (``kimi``).

Adapted from Harbor 0.25.0 (Apache-2.0), agents/installed/kimi_code.py: the
npm install of ``@moonshot-ai/kimi-code``, the ``KIMI_MODEL_*`` env model on an
OpenAI-compatible provider, the ``KIMI_CODE_HOME``, ``KIMI_DISABLE_TELEMETRY``
and ``KIMI_CODE_NO_AUTO_UPDATE`` run env, the
``kimi --prompt ... --output-format stream-json </dev/null`` one-shot run, and
the token usage that Harbor's trajectory reader takes from the session's
``wire.jsonl`` ``usage.record`` entries. Skynet pins the release, checks the
tarball's SHA-256 before it installs, always points the env model at the model
gateway with the gateway token, and blanks the other provider keys.
"""

from __future__ import annotations

from .....exceptions import ServiceError
from .....models.blackbox import BLACKBOX_HARNESS_KIMI_CODE
from ..harness_bridge import parse_pi_output
from . import HarnessSpec, register
from .base import ENV_PROMPT_FILE, GatewayConfig, HarnessLaunch, _base_env, _version_check

KIMI_CODE_ID = BLACKBOX_HARNESS_KIMI_CODE
KIMI_CODE_PACKAGE = "@moonshot-ai/kimi-code"
# npm ``dist-tags.latest`` for ``@moonshot-ai/kimi-code`` on 2026-11-10 (MIT).
KIMI_CODE_VERSION = "2.5.3"
_TARBALL = f"kimi-code-{KIMI_CODE_VERSION}.tgz"
_TARBALL_URL = f"https://registry.npmjs.org/{KIMI_CODE_PACKAGE}/-/{_TARBALL}"
# sha256 of the registry tarball. Its sha512 matches the registry's ``dist.integrity``.
KIMI_CODE_SHA256 = "7c6a44fb54265f9b0d10c35e32c538678a655598f49eba434780c60077fa6c7a"
# A protected run's relay rewrite reaches only launch files, so the gateway URL
# lives in this file and not in the env. It holds no key.
KIMI_CODE_BASE_URL_FILE = ".app/kimi_code/base_url"
# Kimi Code's effort names in 2.5.3: LATEST_OPUS_THINKING_EFFORTS in
# agent-core-v2's Anthropic adapter, which the Gemini adapter also maps. For an
# ``openai`` provider, resolveThinkingEffort sends any name other than ``on``
# and ``off`` unchanged as ``reasoning_effort``. ``off`` is not a Skynet level.
KIMI_CODE_EFFORTS = ("low", "medium", "high", "xhigh", "max")
# Kimi Code prints its stream-json lines on stdout, but they hold no token
# usage. Its stdout goes to a file, and this script prints the answer as one Pi
# ``message_end`` line with the tokens summed from every ``usage.record`` in the
# session journals (``wire.jsonl``, one for each agent), then one Pi
# ``tool_execution_start`` line for each tool call id in the stream, so the
# bridge's cost and tool-call stops read them. An answer's content may be a
# string or a list of text parts. The bridge's usage has only input and output
# tokens, so cache reads and writes count as full input: the count can be high,
# never low, and a token limit stops a run early rather than late. It runs
# inline (``python3 -I -c '...'``) for the reasons :data:`.fx.FX_TO_PI` gives,
# so it must hold no single quote.
KIMI_CODE_TO_PI = """import json
import os
import sys


def records(path):
    try:
        with open(path, encoding="utf-8", errors="replace") as handle:
            lines = handle.read().splitlines()
    except OSError:
        return
    for line in lines:
        try:
            record = json.loads(line)
        except ValueError:
            continue
        if isinstance(record, dict):
            yield record


def count(value):
    return value if type(value) is int and value >= 0 else 0


def answer(content):
    if isinstance(content, list):
        parts = [part.get("text") for part in content if isinstance(part, dict) and part.get("type") == "text"]
        content = "\\n".join(part for part in parts if isinstance(part, str) and part)
    return content.strip() if isinstance(content, str) else ""


text = ""
calls = []
for record in records(sys.argv[1]):
    if record.get("role") != "assistant":
        continue
    text = answer(record.get("content")) or text
    for call in record.get("tool_calls") or []:
        call_id = call.get("id") if isinstance(call, dict) else None
        if isinstance(call_id, str) and call_id not in calls:
            calls.append(call_id)
used = {"input": 0, "output": 0}
for root, _dirs, names in os.walk(sys.argv[2]):
    if "wire.jsonl" not in names:
        continue
    for record in records(os.path.join(root, "wire.jsonl")):
        usage = record.get("usage") if record.get("type") == "usage.record" else None
        if isinstance(usage, dict):
            used["input"] += sum(count(usage.get(key)) for key in ("inputOther", "inputCacheRead", "inputCacheCreation"))
            used["output"] += count(usage.get("output"))
content = [{"type": "text", "text": text}] if text else []
lines = [json.dumps({"type": "message_end", "message": {"role": "assistant", "content": content, "usage": used}})]
lines += [json.dumps({"type": "tool_execution_start", "toolCallId": call_id}) for call_id in calls[:10000]]
sys.stdout.write("\\n".join(lines) + "\\n")
"""
# Kimi Code reads these for its native providers and services. Every launch
# blanks them, so an inherited key or URL never gives it a route around the
# gateway. Kimi Code treats an empty value as unset.
_BLANK_PROVIDER_ENV = dict.fromkeys(
    (
        "OPENAI_API_KEY",
        "OPENAI_BASE_URL",
        "ANTHROPIC_API_KEY",
        "ANTHROPIC_AUTH_TOKEN",
        "ANTHROPIC_BASE_URL",
        "GEMINI_API_KEY",
        "GOOGLE_API_KEY",
        "VERTEXAI_API_KEY",
        "OPENROUTER_API_KEY",
        "KIMI_API_KEY",
        "KIMI_BASE_URL",
        "KIMI_CODE_BASE_URL",
        "KIMI_REGISTRY_API_KEY",
        "KIMI_WEB_SEARCH_API_KEY",
        "KIMI_WEB_SEARCH_BASE_URL",
        "KIMI_WEB_FETCH_API_KEY",
        "KIMI_WEB_FETCH_BASE_URL",
    ),
    "",
)
_KIMI_RUN_ENV = {
    "KIMI_DISABLE_TELEMETRY": "true",
    "KIMI_CODE_NO_AUTO_UPDATE": "true",
    "KIMI_CLI_NO_AUTO_UPDATE": "true",
}


def _kimi_code_install() -> str:
    """Return a command that installs the pinned Kimi Code CLI unless it is already on PATH.

    Harbor runs ``npm install`` from the registry; this downloads the pinned
    tarball, checks its SHA-256, and installs that file. ``--ignore-scripts``
    skips the postinstall, which only renames an old Python ``kimi`` shim.

    Returns:
        The shell command. It removes the download folder on success and on
        failure, and exits non-zero when a step fails.
    """
    # The bootstrap runs this inside its own ``set -eu`` script, so the step
    # records its status with ``|| r=$?`` rather than with an EXIT trap that
    # would replace the caller's.
    return (
        f"{_version_check('kimi', KIMI_CODE_VERSION)} || {{ "
        'd="$(mktemp -d)"; r=0; '
        f'curl -fsSL -o "$d/{_TARBALL}" {_TARBALL_URL} && '
        f'echo "{KIMI_CODE_SHA256}  $d/{_TARBALL}" | sha256sum -c - >/dev/null && '
        f'npm install -g --ignore-scripts "$d/{_TARBALL}" >/dev/null || r=$?; '
        'rm -rf "$d"; [ "$r" -eq 0 ] || exit "$r"; }'
    )


def _effort_env(reasoning_effort: str | None) -> dict[str, str]:
    """Return the ``KIMI_MODEL_THINKING_EFFORT`` setting, or nothing for Kimi Code's default.

    Args:
        reasoning_effort: The user's effort for the model, or ``None``.

    Returns:
        The env entry, or an empty dict.

    Raises:
        ServiceError: When the effort is not in :data:`KIMI_CODE_EFFORTS`.
    """
    if not reasoning_effort:
        return {}
    if reasoning_effort not in KIMI_CODE_EFFORTS:
        raise ServiceError(
            f"The Kimi Code harness does not support the effort '{reasoning_effort}'. "
            f"Pick one of: {', '.join(KIMI_CODE_EFFORTS)}."
        )
    return {"KIMI_MODEL_THINKING_EFFORT": reasoning_effort}


def _kimi_code_launch(model: str, gateway: GatewayConfig, *, reasoning_effort: str | None = None) -> HarnessLaunch:
    """Build the launch for Kimi Code CLI (:data:`KIMI_CODE_PACKAGE`, pinned to :data:`KIMI_CODE_VERSION`).

    Each run gets a fresh ``KIMI_CODE_HOME`` outside the workspace, so the
    session journals never land in the version patch, and an EXIT trap removes
    it. :data:`KIMI_CODE_TO_PI` turns the stream and the journals into Pi lines
    for the bridge's Pi parser. The run keeps Kimi Code's exit status. When
    the parse step fails after Kimi Code succeeds, the run exits 1.

    Args:
        model: Target model id.
        gateway: Gateway URL and key.
        reasoning_effort: The user's effort for the model, or ``None`` for Kimi Code's default.

    Returns:
        The launch.

    Raises:
        ServiceError: When Kimi Code has no effort with that name.
    """
    env = {
        **_base_env(model, gateway),
        **_BLANK_PROVIDER_ENV,
        **_KIMI_RUN_ENV,
        "KIMI_MODEL_NAME": model,
        "KIMI_MODEL_PROVIDER_TYPE": "openai",
        "KIMI_MODEL_API_KEY": gateway.api_key,
        **_effort_env(reasoning_effort),
    }
    run_command = (
        '_kc_dir="$(mktemp -d)" || exit 1; '
        """trap 'rm -rf "$_kc_dir"' EXIT; """
        'export KIMI_CODE_HOME="$_kc_dir/home"; '
        f'KIMI_MODEL_BASE_URL="$(cat {KIMI_CODE_BASE_URL_FILE})" || exit 1; export KIMI_MODEL_BASE_URL; '
        f'kimi --prompt "$(cat "${ENV_PROMPT_FILE}")" --output-format stream-json '
        '< /dev/null > "$_kc_dir/stream.jsonl"; '
        "_kc_status=$?; "
        f'python3 -I -c \'{KIMI_CODE_TO_PI}\' "$_kc_dir/stream.jsonl" "$KIMI_CODE_HOME/sessions" '
        '|| [ "$_kc_status" -ne 0 ] || _kc_status=1; '
        "exit $_kc_status"
    )
    return HarnessLaunch(
        instructions_file="AGENTS.md",
        install_command=_kimi_code_install(),
        files={KIMI_CODE_BASE_URL_FILE: gateway.url},
        run_command=run_command,
        env=env,
        parse_output=parse_pi_output,
    )


SPEC = register(
    HarnessSpec(
        id=KIMI_CODE_ID,
        launch=_kimi_code_launch,
        binary="kimi",
        version=KIMI_CODE_VERSION,
        output_format="pi",
        position=80,
        supported_efforts=KIMI_CODE_EFFORTS,
    )
)
