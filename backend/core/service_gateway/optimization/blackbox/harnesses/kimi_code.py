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
# agent-core-v2's Anthropic adapter, which the Gemini adapter also maps. ``off``
# is not a Skynet level. The effort goes in ``$KIMI_CODE_HOME/config.toml`` as
# ``[thinking] effort``, not in ``KIMI_MODEL_THINKING_EFFORT``: that env name
# sets ``thinking.forcedEffort``, which resolveForcedThinkingEffort applies only
# to a provider type with ``thinkingDriven`` (the ``kimi`` definitions), so an
# ``openai`` provider drops it. ``thinking.effort`` is the new session's level
# (resolveThinkingEffortForModel). The ``openai`` provider has no
# ``strictThinkingValidation``, so the level is not clamped, and the env model's
# default capabilities include ``thinking``, so the OpenAI adapter's
# resolveThinkingEffort sends any level other than ``on`` and ``off`` unchanged
# as ``reasoning_effort``.
KIMI_CODE_EFFORTS = ("low", "medium", "high", "xhigh", "max")
# Kimi Code prints its stream-json lines on stdout, but they hold no token
# usage, and its PromptJsonWriter prints each hook result as a
# ``role: "assistant"`` line with nothing that marks it as a hook. So the run
# drops stdout, and this script reads the session journals (``wire.jsonl``, one
# for each agent) instead. The answer is the text of the last model step in the
# main agent's journal: its ``context.append_loop_event`` records with a
# ``content.part`` event come only from model steps, and hook results are
# ``context.append_message`` records. The script prints the answer as one Pi
# ``message_end`` line with the tokens summed from every ``usage.record`` in
# every journal, then one Pi ``tool_execution_start`` line for each main agent
# ``tool.call`` event, so the bridge's cost and tool-call stops read them.
# The bridge's usage has only input and output tokens, so cache reads and
# writes count as full input: the count can be high, never low, and a token
# limit stops a run early rather than late. It runs
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


steps = {}
calls = []
used = {"input": 0, "output": 0}
for root, dirs, names in os.walk(sys.argv[1]):
    dirs.sort()
    if "wire.jsonl" not in names:
        continue
    main = os.path.basename(root) == "main" and os.path.basename(os.path.dirname(root)) == "agents"
    for record in records(os.path.join(root, "wire.jsonl")):
        usage = record.get("usage") if record.get("type") == "usage.record" else None
        if isinstance(usage, dict):
            used["input"] += sum(count(usage.get(key)) for key in ("inputOther", "inputCacheRead", "inputCacheCreation"))
            used["output"] += count(usage.get("output"))
        event = record.get("event") if record.get("type") == "context.append_loop_event" else None
        if not main or not isinstance(event, dict):
            continue
        part = event.get("part")
        if event.get("type") == "content.part" and isinstance(part, dict) and part.get("type") == "text":
            text = part.get("text")
            if isinstance(text, str):
                steps.setdefault(str(event.get("stepUuid")), []).append(text)
        call_id = event.get("toolCallId") if event.get("type") == "tool.call" else None
        if isinstance(call_id, str) and call_id not in calls:
            calls.append(call_id)
answers = ["".join(parts).strip() for parts in steps.values()]
text = next((answer for answer in reversed(answers) if answer), "")
content = [{"type": "text", "text": text}] if text else []
lines = [json.dumps({"type": "message_end", "message": {"role": "assistant", "content": content, "usage": used}})]
lines += [json.dumps({"type": "tool_execution_start", "toolCallId": call_id}) for call_id in calls[:10000]]
sys.stdout.write("\\n".join(lines) + "\\n")
"""
# Kimi Code 2.5.3 print mode sends a prompt whose trimmed text starts with
# ``/goal`` and a space (or ends there) to its headless goal parser
# (parseHeadlessGoalCreate, GOAL_PREFIX), not to the model. It has no option to
# turn that off, so, like Harbor does for Code Puppy, the run frames such a
# prompt and leaves every other prompt byte-identical. JavaScript's ``trim()``
# also strips Unicode whitespace and U+FEFF, so the check runs in Python. It
# exits 0 only for a prompt the goal parser does not take, so a failed check
# frames the prompt.
_PLAIN_PROMPT_CHECK = (
    "import re, sys; "
    'text = open(sys.argv[1], encoding="utf-8", errors="replace").read(); '
    'sys.exit(bool(re.match(r"[\\s\\ufeff]*/goal(?:[\\s\\ufeff]|$)", text)))'
)
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
# Every ``KIMI_MODEL_*`` name that Kimi Code 2.5.3 reads (its env model overlay,
# the provider and thinking env bindings, and the prompt output format). Every
# launch sets each one, blank when Skynet does not use it, so an inherited value
# cannot change the model, its limits or its effort. Kimi Code treats a blank
# value as unset. The run command sets ``KIMI_MODEL_BASE_URL`` from
# :data:`KIMI_CODE_BASE_URL_FILE`.
_BLANK_MODEL_ENV = dict.fromkeys(
    (
        "KIMI_MODEL_ADAPTIVE_THINKING",
        "KIMI_MODEL_API_KEY",
        "KIMI_MODEL_BASE_URL",
        "KIMI_MODEL_CAPABILITIES",
        "KIMI_MODEL_DISPLAY_NAME",
        "KIMI_MODEL_MAX_COMPLETION_TOKENS",
        "KIMI_MODEL_MAX_CONTEXT_SIZE",
        "KIMI_MODEL_MAX_OUTPUT_SIZE",
        "KIMI_MODEL_MAX_TOKENS",
        "KIMI_MODEL_NAME",
        "KIMI_MODEL_OUTPUT_FORMAT",
        "KIMI_MODEL_PROVIDER_TYPE",
        "KIMI_MODEL_REASONING_KEY",
        "KIMI_MODEL_TEMPERATURE",
        "KIMI_MODEL_THINKING_EFFORT",
        "KIMI_MODEL_THINKING_KEEP",
        "KIMI_MODEL_TOP_P",
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


def _effort_config(reasoning_effort: str | None) -> str:
    """Return a command that writes the effort into Kimi Code's ``config.toml``, or nothing for its default.

    Args:
        reasoning_effort: The user's effort for the model, or ``None``.

    Returns:
        The shell command, or an empty string. It exits non-zero when the write fails.

    Raises:
        ServiceError: When the effort is not in :data:`KIMI_CODE_EFFORTS`.
    """
    if not reasoning_effort:
        return ""
    if reasoning_effort not in KIMI_CODE_EFFORTS:
        raise ServiceError(
            f"The Kimi Code harness does not support the effort '{reasoning_effort}'. "
            f"Pick one of: {', '.join(KIMI_CODE_EFFORTS)}."
        )
    return (
        'mkdir -p "$KIMI_CODE_HOME" && '
        f"printf '%s\\n' '[thinking]' 'effort = \"{reasoning_effort}\"' > \"$KIMI_CODE_HOME/config.toml\" || exit 1; "
    )


def _kimi_code_launch(model: str, gateway: GatewayConfig, *, reasoning_effort: str | None = None) -> HarnessLaunch:
    """Build the launch for Kimi Code CLI (:data:`KIMI_CODE_PACKAGE`, pinned to :data:`KIMI_CODE_VERSION`).

    Each run gets a fresh ``KIMI_CODE_HOME`` outside the workspace, so the
    session journals never land in the version patch, and an EXIT trap removes
    it. :data:`KIMI_CODE_TO_PI` turns the journals into Pi lines for the
    bridge's Pi parser. The run keeps Kimi Code's exit status. When the parse
    step fails after Kimi Code succeeds, the run exits 1.

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
        **_BLANK_MODEL_ENV,
        **_KIMI_RUN_ENV,
        "KIMI_MODEL_NAME": model,
        "KIMI_MODEL_PROVIDER_TYPE": "openai",
        "KIMI_MODEL_API_KEY": gateway.api_key,
    }
    # Kimi Code 2.5.3 takes the prompt only as the ``--prompt`` value: print
    # mode has no prompt-file option and does not read stdin. So, unlike Pi's
    # ``@file``, the prompt is one argv string, and Linux refuses a single
    # argument over 128 KiB (MAX_ARG_STRLEN) with E2BIG before Kimi Code starts.
    run_command = (
        '_kc_dir="$(mktemp -d)" || exit 1; '
        """trap 'rm -rf "$_kc_dir"' EXIT; """
        'export KIMI_CODE_HOME="$_kc_dir/home"; '
        f"{_effort_config(reasoning_effort)}"
        f'KIMI_MODEL_BASE_URL="$(cat {KIMI_CODE_BASE_URL_FILE})" || exit 1; export KIMI_MODEL_BASE_URL; '
        f'_kc_prompt="$(cat "${ENV_PROMPT_FILE}")"; '
        f"python3 -I -c '{_PLAIN_PROMPT_CHECK}' \"${ENV_PROMPT_FILE}\" || "
        '_kc_prompt="Task request:\n$_kc_prompt"; '
        'kimi --prompt "$_kc_prompt" --output-format stream-json < /dev/null > /dev/null; '
        "_kc_status=$?; "
        f"python3 -I -c '{KIMI_CODE_TO_PI}' \"$KIMI_CODE_HOME/sessions\" "
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
