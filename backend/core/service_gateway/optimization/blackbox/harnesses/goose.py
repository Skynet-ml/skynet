"""Goose CLI harness (``goose``).

Adapted from Harbor 0.25.0 (Apache-2.0), agents/installed/goose: the release
binary install, the keyring-free headless setup, the ``GOOSE_THINKING_EFFORT``
setting and the ``goose run`` one-shot with the developer extension. Harbor
pipes the upstream ``download_cli.sh`` into bash, gives goose a direct
provider key and runs a generated recipe; Skynet pins one release, checks its
sha256 digest, passes the prompt file with ``-i``, and points goose's
OpenAI-compatible provider at the model gateway instead.
"""

from __future__ import annotations

from .....exceptions import ServiceError
from .....models.blackbox import BLACKBOX_HARNESS_GOOSE
from ..harness_bridge import parse_plain_output
from . import HarnessSpec, register
from .base import ENV_API_KEY, ENV_MODEL, ENV_PROMPT_FILE, GatewayConfig, HarnessLaunch, _base_env, _version_check

# GitHub release ``v1.57.0`` of aaif-goose/goose (Apache-2.0), 2026-10-28.
GOOSE_VERSION = "1.57.0"
GOOSE_PROVIDER = "openai"
_RELEASE_URL = f"https://github.com/aaif-goose/goose/releases/download/v{GOOSE_VERSION}"
# sha256 digests the GitHub release API reports for the musl tarballs, so a
# swapped asset fails the install instead of running.
GOOSE_SHA256 = {
    "x86_64": "6c3a05e30e1a425dc06a410dbe24b3f3697c9721b1ea2340434ddda6b64267ae",
    "aarch64": "00a32522f0421ed72fc06f36bfc44c8e51cffefb4daf22a81bb84ccd29d1a1ce",
}
# The gateway URL lives in this file rather than on the command line, so a
# protected run's relay rewrite of generated files reaches it.
GOOSE_BASE_URL_FILE = ".app/goose/base_url"
_LOCAL_BIN = '"$HOME/.local/bin"'
# Skynet's effort names that goose 1.57.0 has its own level for
# (``ThinkingEffort::from_str``). Goose reads ``none`` as its ``off``, the same
# level spelled differently. It reads ``xhigh`` as ``max``, a different Skynet
# level, and has no ``minimal``, so those two are refused, not remapped.
GOOSE_THINKING_LEVELS = frozenset({"none", "low", "medium", "high", "max"})
# Goose reads these keys for its other providers. A protected run blanks them,
# so an inherited key never gives goose a route around the gateway.
_PROTECTED_ENV = {
    "ANTHROPIC_API_KEY": "",
    "GOOGLE_API_KEY": "",
    "GEMINI_API_KEY": "",
    "OPENROUTER_API_KEY": "",
    "DATABRICKS_TOKEN": "",
    "GROQ_API_KEY": "",
    "XAI_API_KEY": "",
    "MISTRAL_API_KEY": "",
    "DEEPSEEK_API_KEY": "",
    "AZURE_OPENAI_API_KEY": "",
    # Telemetry is opt-in upstream; these and the launch's GOOSE_TELEMETRY_OFF
    # keep it off even if a config enables it. Goose has no auto-update, only
    # the manual ``goose update`` command.
    "GOOSE_TELEMETRY_ENABLED": "false",
    "DO_NOT_TRACK": "1",
    # With the toolshim on, goose 1.57.0 sends tool-call interpretation to
    # OLLAMA_HOST, a model route outside the gateway.
    "GOOSE_TOOLSHIM": "false",
}


def _goose_install() -> str:
    """Return a command that installs the pinned goose release unless it is already on PATH.

    Returns:
        The shell command. It downloads one tarball and checks its sha256
        digest before it unpacks the binary into ``$HOME/.local/bin``. It
        removes the download folder on success and on failure, and exits
        non-zero when a step fails.
    """
    arch_cases = " ".join(
        f"{pattern}) a={arch}; s={GOOSE_SHA256[arch]};;"
        for pattern, arch in (("x86_64|amd64", "x86_64"), ("aarch64|arm64", "aarch64"))
    )
    # The bootstrap runs this inside its own ``set -eu`` script, so the step
    # records its status with ``|| r=$?`` (exempt from ``set -e``) and removes
    # the download folder before it fails, rather than with an EXIT trap that
    # would replace the caller's.
    return (
        f'export PATH={_LOCAL_BIN}:"$PATH"; '
        f"{_version_check('goose', GOOSE_VERSION)} || {{ "
        f'case "$(uname -m)" in {arch_cases} *) echo "goose: unsupported architecture" >&2; exit 1;; esac; '
        'f="goose-$a-unknown-linux-musl.tar.gz"; d="$(mktemp -d)"; r=0; '
        f'curl -fsSL -o "$d/$f" "{_RELEASE_URL}/$f" && '
        'echo "$s  $d/$f" | sha256sum -c - >/dev/null && '
        'tar -xzf "$d/$f" -C "$d" && '
        f"mkdir -p {_LOCAL_BIN} && "
        f'install -m 755 "$d/goose" {_LOCAL_BIN}/goose || r=$?; '
        'rm -rf "$d"; [ "$r" -eq 0 ] || exit "$r"; }'
    )


def _goose_thinking(reasoning_effort: str | None) -> dict[str, str]:
    """Return the ``GOOSE_THINKING_EFFORT`` setting for the user's effort, or nothing for goose's default.

    Args:
        reasoning_effort: The user's effort for the model, or ``None``.

    Returns:
        The env entry with the effort unchanged, or an empty dict.

    Raises:
        ServiceError: When goose has no level for the effort.
    """
    if not reasoning_effort:
        return {}
    if reasoning_effort not in GOOSE_THINKING_LEVELS:
        supported = ", ".join(sorted(GOOSE_THINKING_LEVELS))
        raise ServiceError(f"Goose does not support effort {reasoning_effort!r}. Supported efforts: {supported}.")
    return {"GOOSE_THINKING_EFFORT": reasoning_effort}


def _goose_launch(model: str, gateway: GatewayConfig, *, reasoning_effort: str | None = None) -> HarnessLaunch:
    """Build the launch for Goose CLI (``goose``, pinned to :data:`GOOSE_VERSION`).

    Each run gets a fresh goose root directory outside the workspace, so the
    session and log files never land in the files the run hands back. An EXIT
    trap removes it when the run's shell exits and keeps goose's exit status.
    Goose's quiet one-shot output is the answer; the bridge has no parser for
    its stream-json format.

    Args:
        model: Target model id.
        gateway: Gateway URL and key.
        reasoning_effort: The user's effort for the model, or ``None`` for goose's default.

    Returns:
        The launch.

    Raises:
        ServiceError: When goose has no level for the effort.
    """
    # Goose 1.57.0 sends gpt-5*, gpt-6* and o* models to the Responses API
    # when the base path is the default ``v1/chat/completions``
    # (``OpenAiProvider::should_use_responses_api``), and Skynet's local CLI
    # and BYOK routes reject /responses. OPENAI_BASE_URL cannot avoid this: for
    # a URL that ends in /v1, ``parse_openai_base_url`` strips the /v1 and
    # ``openai_def`` defaults the path back to ``v1/chat/completions``. So the
    # whole gateway URL goes in OPENAI_HOST, which wins over OPENAI_BASE_URL
    # (``resolve_base_url``), with the custom path ``chat/completions``. A
    # custom chat path forces chat completions, and ``ApiClient::build_url``
    # appends it to the gateway URL, path prefix included.
    run_command = (
        f'export PATH={_LOCAL_BIN}:"$PATH" && '
        'export GOOSE_PATH_ROOT="$(mktemp -d)" && '
        "trap 'rm -rf \"$GOOSE_PATH_ROOT\"' EXIT && "
        f'OPENAI_HOST="$(cat {GOOSE_BASE_URL_FILE})" OPENAI_BASE_PATH=chat/completions '
        f'OPENAI_API_KEY="${ENV_API_KEY}" '
        "goose run --no-session --quiet --no-profile --with-builtin developer "
        f'--provider {GOOSE_PROVIDER} --model "${ENV_MODEL}" -i "${ENV_PROMPT_FILE}" < /dev/null'
    )
    env = _base_env(model, gateway)
    env.update(
        {
            "GOOSE_DISABLE_KEYRING": "1",
            "GOOSE_TELEMETRY_OFF": "1",
            "GOOSE_MODE": "auto",
            **_goose_thinking(reasoning_effort),
        }
    )
    return HarnessLaunch(
        instructions_file="AGENTS.md",
        install_command=_goose_install(),
        files={GOOSE_BASE_URL_FILE: gateway.url},
        run_command=run_command,
        env=env,
        parse_output=parse_plain_output,
    )


SPEC = register(
    HarnessSpec(
        id=BLACKBOX_HARNESS_GOOSE,
        launch=_goose_launch,
        binary="goose",
        version=GOOSE_VERSION,
        output_format="plain",
        protected_env=_PROTECTED_ENV,
        position=64,
    )
)
