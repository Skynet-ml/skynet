"""OpenCode V2 harness (``@opencode/cli``, run as ``opencode2``).

Adapted from Harbor 0.25.0 (Apache-2.0), agents/installed/opencode_v2: the
``opencode2`` binary name beside V1's ``opencode``, the V2 ``providers``
config with the native openai-compatible package, effort as the
``provider/model#variant`` model suffix, and ``opencode2 run --standalone
--format json --auto``. Harbor installs the npm package and passes the
config in ``OPENCODE_CONFIG_CONTENT``; Skynet pins one GitHub release, checks
its sha256 digest, and writes the config to a workspace file, so a protected
run's relay rewrite reaches the gateway URL.
"""

from __future__ import annotations

import json

from .....exceptions import ServiceError
from .....models.blackbox import BLACKBOX_HARNESS_OPENCODE_V2
from ..harness_bridge import parse_opencode_output
from . import HarnessSpec, register
from .base import (
    _PROMPT_ARG,
    ENV_API_KEY,
    ENV_MODEL,
    PROVIDER,
    GatewayConfig,
    HarnessLaunch,
    _base_env,
    _version_check,
)

# GitHub release ``v2.2.7`` of anomalyco/opencode (MIT), 2026-11-10.
OPENCODE_V2_VERSION = "2.2.7"
# ``opencode2 --version`` prints ``opencode v2.2.7`` (effect 4.1.0
# ``CliOutput.defaultFormatter.formatVersion``), so the pinned check matches
# the ``v``-prefixed release.
OPENCODE_V2_VERSION_OUTPUT = f"v{OPENCODE_V2_VERSION}"
# V2's npm package also installs a global ``opencode`` bin, which would take
# V1's name; the ``opencode2`` alias keeps both harnesses on one PATH.
OPENCODE_V2_BINARY = "opencode2"
_RELEASE_URL = f"https://github.com/anomalyco/opencode/releases/download/v{OPENCODE_V2_VERSION}"
# sha256 digests the GitHub release API reports for the glibc tarballs, so a
# swapped asset fails the install instead of running. Linux x64 uses the
# ``-baseline`` build, as the release's Homebrew recipe does, so a CPU
# without AVX2 can run it.
OPENCODE_V2_SHA256 = {
    "x64-baseline": "89a6cee227b54bc011179c528a5e63176032462b1a5e21723b616801cddc2ca7",
    "arm64": "c1682057f0815cb2abdd6e00305f1146ebaf1c167f0335cdb74ddc80008120e7",
}
# The variants V2 2.2.7 builds for a config model on the openai-compatible
# package with no ``capabilities.reasoning``: the ``PROTOCOLS`` table maps the
# package to ``openaiCompatible``, which hands a non-nvidia provider to
# ``openaiChat`` and its ``EFFORTS`` (packages/core/src/variant.ts).
# Any other ``#variant`` fails with ``VariantUnavailableError``
# (packages/core/src/model-resolver.ts).
OPENCODE_V2_EFFORTS = ("low", "medium", "high")
_LOCAL_BIN = '"$HOME/.local/bin"'
# Every run gets these. The built-in ``opencode`` provider serves free models
# with no key, a model route outside the gateway, and the updater and the
# OTLP exporter reach the internet on their own.
_ENV = {
    "OPENCODE_DISABLE_AUTOUPDATE": "true",
    "OPENCODE_DISABLE_FREE_MODELS": "true",
    "OTEL_EXPORTER_OTLP_ENDPOINT": "",
}
# OpenCode reads these keys for its other providers. A protected run blanks
# them, so an inherited key never gives OpenCode a route around the gateway.
# A protected sandbox has no internet route, so the models.dev fetch is off.
# V2 merges ``OPENCODE_CONFIG`` and ``OPENCODE_CONFIG_CONTENT`` into the
# workspace config, so an inherited one could add a provider. An empty
# object, not an empty string, because V2 parses any set content as JSON.
_PROTECTED_ENV = {
    "OPENCODE_CONFIG": "",
    "OPENCODE_CONFIG_CONTENT": "{}",
    "OPENCODE_API_KEY": "",
    "OPENAI_API_KEY": "",
    "ANTHROPIC_API_KEY": "",
    "GOOGLE_GENERATIVE_AI_API_KEY": "",
    "GEMINI_API_KEY": "",
    "OPENROUTER_API_KEY": "",
    "GROQ_API_KEY": "",
    "XAI_API_KEY": "",
    "MISTRAL_API_KEY": "",
    "DEEPSEEK_API_KEY": "",
    "AZURE_API_KEY": "",
    "GITHUB_TOKEN": "",
    "OPENCODE_DISABLE_MODELS_FETCH": "true",
}


def _check_effort(reasoning_effort: str) -> None:
    """Refuse an effort that OpenCode V2 has no variant for.

    Args:
        reasoning_effort: The user's effort for the model.

    Raises:
        ServiceError: When the effort is not in :data:`OPENCODE_V2_EFFORTS`.
    """
    if reasoning_effort not in OPENCODE_V2_EFFORTS:
        raise ServiceError(
            f"The OpenCode V2 harness does not support the effort '{reasoning_effort}'. "
            f"Pick one of: {', '.join(OPENCODE_V2_EFFORTS)}."
        )


def _opencode_v2_install() -> str:
    """Return a command that installs the pinned OpenCode V2 release unless it is already on PATH.

    Returns:
        The shell command. It downloads one tarball and checks its sha256
        digest before it unpacks the binary into ``$HOME/.local/bin`` as
        ``opencode2``. It removes the download folder on success and on
        failure, and exits non-zero when a step fails.
    """
    arch_cases = " ".join(
        f"{pattern}) a={arch}; s={OPENCODE_V2_SHA256[arch]};;"
        for pattern, arch in (("x86_64|amd64", "x64-baseline"), ("aarch64|arm64", "arm64"))
    )
    # The bootstrap runs this inside its own ``set -eu`` script, so the step
    # records its status with ``|| r=$?`` (exempt from ``set -e``) and removes
    # the download folder before it fails, rather than with an EXIT trap that
    # would replace the caller's.
    return (
        f'export PATH={_LOCAL_BIN}:"$PATH"; '
        f"{_version_check(OPENCODE_V2_BINARY, OPENCODE_V2_VERSION_OUTPUT)} || {{ "
        f'case "$(uname -m)" in {arch_cases} *) echo "opencode2: unsupported architecture" >&2; exit 1;; esac; '
        'f="opencode-linux-$a.tar.gz"; d="$(mktemp -d)"; r=0; '
        f'curl -fsSL -o "$d/$f" "{_RELEASE_URL}/$f" && '
        'echo "$s  $d/$f" | sha256sum -c - >/dev/null && '
        'tar -xzf "$d/$f" -C "$d" && '
        f"mkdir -p {_LOCAL_BIN} && "
        f'install -m 755 "$d/opencode" {_LOCAL_BIN}/{OPENCODE_V2_BINARY} || r=$?; '
        'rm -rf "$d"; [ "$r" -eq 0 ] || exit "$r"; }'
    )


def _opencode_v2_launch(model: str, gateway: GatewayConfig, *, reasoning_effort: str | None = None) -> HarnessLaunch:
    """Build the launch for OpenCode V2 (``opencode2``, pinned to :data:`OPENCODE_V2_VERSION`).

    Args:
        model: Target model id.
        gateway: Gateway URL and key.
        reasoning_effort: The user's effort for the model, or ``None`` for OpenCode's default.

    Returns:
        The launch.

    Raises:
        ServiceError: When OpenCode V2 has no variant with that name.
    """
    variant = ""
    if reasoning_effort:
        _check_effort(reasoning_effort)
        variant = f"#{reasoning_effort}"
    config = {
        "update": "disable",
        "share": "disabled",
        "providers": {
            PROVIDER: {
                "name": "Model gateway",
                "package": "@opencode/ai/providers/openai-compatible",
                "settings": {"baseURL": gateway.url, "apiKey": f"{{env:{ENV_API_KEY}}}"},
                "models": {model: {"name": model}},
            }
        },
        "model": f"{PROVIDER}/{model}",
    }
    # ``--standalone`` runs a private server for this run instead of a shared
    # background service; ``--auto`` approves tool permissions with no prompt.
    run_command = (
        f'export PATH={_LOCAL_BIN}:"$PATH" && '
        f"{OPENCODE_V2_BINARY} run --standalone --format json --auto "
        f'--model "{PROVIDER}/${ENV_MODEL}{variant}" -- {_PROMPT_ARG} < /dev/null'
    )
    env = _base_env(model, gateway)
    env.update(_ENV)
    return HarnessLaunch(
        instructions_file="AGENTS.md",
        install_command=_opencode_v2_install(),
        files={"opencode.json": json.dumps(config, indent=2)},
        run_command=run_command,
        env=env,
        parse_output=parse_opencode_output,
    )


SPEC = register(
    HarnessSpec(
        id=BLACKBOX_HARNESS_OPENCODE_V2,
        launch=_opencode_v2_launch,
        binary=OPENCODE_V2_BINARY,
        version=OPENCODE_V2_VERSION_OUTPUT,
        output_format="opencode",
        protected_env=_PROTECTED_ENV,
        position=32,
        supported_efforts=OPENCODE_V2_EFFORTS,
    )
)
