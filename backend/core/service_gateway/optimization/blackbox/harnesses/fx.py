"""Vercel fx coding-agent harness (``fx``).

Adapted from Harbor 0.25.0 (Apache-2.0), agents/installed/fx: the
``fx.sh/setup.sh`` install with a version argument into ``~/.local/bin``, the
``~/.fx/settings.json`` custom ``openai-chat-completions`` provider, the
``FX_*`` run env and the ``fx ask --yolo -- <prompt> < /dev/null`` one-shot
run. Harbor sends fx to the Vercel AI Gateway with ``AI_GATEWAY_API_KEY``;
Skynet always uses the custom provider, points it at the model gateway, and
reads the gateway token from the Skynet env instead.
"""

from __future__ import annotations

import json

from .....models.blackbox import BLACKBOX_HARNESS_FX
from ..harness_bridge import parse_plain_output
from . import HarnessSpec, register
from .base import _PROMPT_ARG, ENV_API_KEY, GatewayConfig, HarnessLaunch, _base_env, _version_check

# ``https://releases.fx.sh/latest.txt`` on 2026-11-08 (release tag ``v0.0.16``).
FX_VERSION = "0.0.16"
FX_INSTALL_SCRIPT = "https://fx.sh/setup.sh"
# The name fx's settings give the custom provider; ``FX_PROVIDER`` selects it.
FX_PROVIDER = "runtime"
# The gateway URL lives in this file rather than on the command line, so a
# protected run's relay rewrite of generated files reaches it before the copy.
FX_SETTINGS_FILE = ".app/fx/settings.json"
_FX_PATH = 'export PATH="$HOME/.local/bin:$PATH"'
# fx's own ``effort`` values; Skynet sends no effort for any other name.
_FX_EFFORTS = frozenset({"auto", "none", "minimal", "low", "medium", "high", "xhigh", "max"})
# fx reads these to reach the Vercel AI Gateway directly. A protected run
# blanks them, so an inherited key never gives fx a route around the gateway.
_PROTECTED_ENV = {
    "AI_GATEWAY_API_KEY": "",
    "VERCEL_AI_GATEWAY_API_KEY": "",
    "VERCEL_OIDC_TOKEN": "",
    "FX_GATEWAY_BASE_URL": "",
    "FX_GATEWAY_CHAT_URL": "",
    "FX_E2E_GATEWAY_CHAT_URL": "",
    # fx documents no telemetry switch; this is the generic opt-out.
    "DO_NOT_TRACK": "1",
}


def _fx_install() -> str:
    """Return the command that pins fx to :data:`FX_VERSION` and logs the version that will run.

    Returns:
        The shell command.
    """
    return (
        f"{_FX_PATH}; {_version_check('fx', FX_VERSION)} "
        f"|| curl -fsSL {FX_INSTALL_SCRIPT} | bash -s -- v{FX_VERSION} >/dev/null && fx --version"
    )


def _fx_settings(gateway: GatewayConfig, reasoning_effort: str | None) -> dict[str, object]:
    """Build fx's ``settings.json`` with one custom provider that is the model gateway.

    Args:
        gateway: Gateway URL and key.
        reasoning_effort: The user's effort for the model, or ``None`` for fx's default.

    Returns:
        The settings content.
    """
    settings: dict[str, object] = {
        "providers": {
            FX_PROVIDER: {
                "protocol": "openai-chat-completions",
                "base_url": gateway.url,
                "auth": {"type": "bearer", "env": ENV_API_KEY},
            }
        }
    }
    if reasoning_effort in _FX_EFFORTS:
        settings["effort"] = reasoning_effort
    return settings


def _fx_launch(model: str, gateway: GatewayConfig, *, reasoning_effort: str | None = None) -> HarnessLaunch:
    """Build the launch for fx (``fx``, pinned to :data:`FX_VERSION`).

    fx's plain one-shot output is the answer; the bridge has no parser for
    its ``--json`` output.

    Args:
        model: Target model id.
        gateway: Gateway URL and key.
        reasoning_effort: The user's effort for the model, or ``None`` for fx's default.

    Returns:
        The launch.
    """
    env = _base_env(model, gateway)
    env.update({"FX_MODEL": model, "FX_PROVIDER": FX_PROVIDER, "FX_AUTO_UPGRADE": "0", "FX_SOUND": "0"})
    return HarnessLaunch(
        instructions_file="AGENTS.md",
        install_command=_fx_install(),
        files={FX_SETTINGS_FILE: json.dumps(_fx_settings(gateway, reasoning_effort), indent=2)},
        run_command=(
            f'{_FX_PATH}; mkdir -p "$HOME/.fx" && cp {FX_SETTINGS_FILE} "$HOME/.fx/settings.json" && '
            f"fx ask --yolo -- {_PROMPT_ARG} < /dev/null"
        ),
        env=env,
        parse_output=parse_plain_output,
    )


SPEC = register(
    HarnessSpec(
        id=BLACKBOX_HARNESS_FX,
        launch=_fx_launch,
        binary="fx",
        version=FX_VERSION,
        output_format="plain",
        protected_env=_PROTECTED_ENV,
        position=62,
    )
)
