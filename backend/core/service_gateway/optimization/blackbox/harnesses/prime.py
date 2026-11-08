"""Prime Agent harness, a Pi fork that ships as a versioned tarball on its own release CDN."""

from __future__ import annotations

import json

from .....models.blackbox import BLACKBOX_HARNESS_PRIME
from ..harness_bridge import parse_pi_output
from . import HarnessSpec, register
from .base import _GUARD_PI_ARG, _PROMPT_ARG, PROVIDER, GatewayConfig, HarnessLaunch, _base_env
from .pi import _pi_models_json

# Prime Agent is a Pi fork that ships as a versioned npm tarball on its own
# release CDN (``<base>/latest.json`` names the current one), not on npm.
PRIME_AGENT_VERSION = "0.9.7"
PRIME_AGENT_TARBALL = (
    "https://pub-728493de92a943e2a9b2d17b4719f318.r2.dev"
    f"/releases/v{PRIME_AGENT_VERSION}/prime-agent-{PRIME_AGENT_VERSION}.tgz"
)
# Its config dir; the run points ``PRIME_AGENT_CODING_AGENT_DIR`` here so the
# models.json the box receives is the one it reads.
_PRIME_AGENT_DIR = ".app/prime"
# The sandbox image ships a prebuilt kernel here because protected sandboxes
# cannot download one; elsewhere it is absent and Prime Agent bootstraps its own.
PRIME_AGENT_IMAGE_KERNEL = "/opt/prime-agent/kernel-venv/bin/python"


def _prime_agent_install() -> str:
    """Return the command that pins Prime Agent to :data:`PRIME_AGENT_VERSION` and logs the version that will run.

    The tarball's postinstall builds the Python kernel (uv, Python 3.11 and the
    runtime) when told to, so the first case does not pay for it.

    Returns:
        The shell command.
    """
    return (
        f'[ "$(prime-agent --version 2>/dev/null)" = "{PRIME_AGENT_VERSION}" ] '
        "|| PRIME_AGENT_BOOTSTRAP_KERNEL_ON_INSTALL=1 npm install -g --no-fund --no-audit --loglevel=error "
        f'--progress=false "{PRIME_AGENT_TARBALL}" && prime-agent --version'
    )


def _prime_agent_launch(model: str, gateway: GatewayConfig, *, reasoning_effort: str | None = None) -> HarnessLaunch:
    """Build the launch for Prime Agent (:data:`PRIME_AGENT_TARBALL`, pinned to :data:`PRIME_AGENT_VERSION`).

    Prime Agent works through a Python kernel. The sandbox image's prebuilt
    :data:`PRIME_AGENT_IMAGE_KERNEL` is used when present; an empty
    ``PRIME_AGENT_KERNEL_PYTHON`` otherwise lets it bootstrap one with uv, and
    ``PRIME_AGENT_INSTALL_UV`` lets that happen without a terminal to confirm
    on. Its JSON stream has Pi's shape, so Pi's parser reads it.

    Args:
        model: Target model id.
        gateway: Gateway URL and key.
        reasoning_effort: The user's effort for the model, or ``None`` for the default.

    Returns:
        The launch.
    """
    models_json, model_arg = _pi_models_json(model, gateway, reasoning_effort=reasoning_effort)
    env = _base_env(model, gateway)
    env.update({"PRIME_AGENT_INSTALL_UV": "1", "PRIME_AGENT_TELEMETRY": "0", "PI_SKIP_VERSION_CHECK": "1"})
    return HarnessLaunch(
        instructions_file="AGENTS.md",
        install_command=_prime_agent_install(),
        files={f"{_PRIME_AGENT_DIR}/models.json": json.dumps(models_json, indent=2)},
        run_command=(
            f'PRIME_AGENT_CODING_AGENT_DIR="$PWD/{_PRIME_AGENT_DIR}" '
            f'PRIME_AGENT_KERNEL_PYTHON="$([ -x {PRIME_AGENT_IMAGE_KERNEL} ] && echo {PRIME_AGENT_IMAGE_KERNEL})" '
            f"prime-agent --mode json --no-session --provider {PROVIDER} --model {model_arg} {_GUARD_PI_ARG}{_PROMPT_ARG}"
        ),
        env=env,
        parse_output=parse_pi_output,
    )


SPEC = register(
    HarnessSpec(
        id=BLACKBOX_HARNESS_PRIME,
        launch=_prime_agent_launch,
        binary="prime-agent",
        version=PRIME_AGENT_VERSION,
        output_format="pi",
        position=40,
    )
)
