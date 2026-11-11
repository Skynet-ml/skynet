"""Vercel fx coding-agent harness (``fx``).

Adapted from Harbor 0.25.0 (Apache-2.0), agents/installed/fx: the install
into ``~/.local/bin``, the ``~/.fx/settings.json`` custom
``openai-chat-completions`` provider, the ``FX_*`` run env and the
``fx ask --yolo --json -- <prompt> < /dev/null`` one-shot run. Harbor pipes
``fx.sh/setup.sh`` into a shell; Skynet downloads the pinned release tarball
and checks its SHA-256 before it installs the binary. Harbor sends fx to the
Vercel AI Gateway with ``AI_GATEWAY_API_KEY``; Skynet always uses the custom
provider, points it at the model gateway, and reads the gateway token from
the Skynet env instead.
"""

from __future__ import annotations

import json

from .....exceptions import ServiceError
from .....models.blackbox import BLACKBOX_HARNESS_FX
from ..harness_bridge import parse_pi_output
from . import HarnessSpec, register
from .base import _PROMPT_ARG, ENV_API_KEY, GatewayConfig, HarnessLaunch, _base_env, _version_check

# ``https://releases.fx.sh/latest.txt`` on 2026-11-08 (release tag ``v0.0.16``).
FX_VERSION = "0.0.16"
FX_RELEASE_URL = f"https://releases.fx.sh/v{FX_VERSION}"
# From ``{FX_RELEASE_URL}/fx-linux-<arch>.tar.gz.sha256``; the ``digest`` of the
# same assets on github.com/vercel-labs/fx/releases/tag/v0.0.16 matches.
FX_SHA256 = {
    "x86_64": "f0fb822c7be546c80bcc89d50521bc1454435e0c6f586e8420c4e2b84fa80599",
    "aarch64": "ca9441b104357e2105039dbfd4c2eaf0d6ee65a84085f23f51e880c95cbf46af",
}
# The name fx's settings give the custom provider; ``FX_PROVIDER`` selects it.
FX_PROVIDER = "runtime"
# The gateway URL lives in this file rather than on the command line, so a
# protected run's relay rewrite of generated files reaches it before the copy.
FX_SETTINGS_FILE = ".app/fx/settings.json"
# ``fx ask --json`` prints one result object at the end; only a completed turn
# fills ``final_output``, while ``output`` also holds interim text. The bridge
# has no fx parser, so this script reads the object with ``json`` and prints one
# Pi ``message_end`` line, the shape the Antigravity runner also uses. A script
# in ``files`` would land in the working directory that ``fx --yolo`` can edit
# and would go through a protected run's relay rewrite, so it runs inline
# (``python3 -I -c '...'``) and must hold no single quote.
FX_TO_PI = """import json
import sys

result = None
try:
    with open(sys.argv[1], encoding="utf-8") as handle:
        lines = handle.read().splitlines()
except OSError:
    lines = []
for line in lines:
    line = line.strip()
    if not line.startswith("{"):
        continue
    try:
        event = json.loads(line)
    except ValueError:
        continue
    if isinstance(event, dict) and ("final_output" in event or "usage" in event):
        result = event


def count(value):
    return value if type(value) is int and value >= 0 else 0


if result is not None:
    used = result.get("usage") if isinstance(result.get("usage"), dict) else {}
    text = result.get("final_output")
    text = text.strip() if isinstance(text, str) else ""
    usage = {"input": count(used.get("input_tokens")), "output": count(used.get("output_tokens"))}
    if result.get("error"):
        print(json.dumps({"type": "error", "message": str(result["error"])[:500]}))
    content = [{"type": "text", "text": text}] if text else []
    print(json.dumps({"type": "message_end", "message": {"role": "assistant", "content": content, "usage": usage}}))
"""
_FX_PATH = 'export PATH="$HOME/.local/bin:$PATH"'
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
    """Return the command that installs the pinned fx release after a SHA-256 check and logs its version.

    The tarball goes to a fresh temporary directory. ``sha256sum -c`` fails
    the whole command on a mismatch, so a changed asset never reaches
    ``~/.local/bin``.

    Returns:
        The shell command.
    """
    return (
        f"{_FX_PATH}; {_version_check('fx', FX_VERSION)} || {{ "
        'case "$(uname -m)" in '
        f"x86_64|amd64) a=x86_64 h={FX_SHA256['x86_64']};; "
        f"aarch64|arm64) a=aarch64 h={FX_SHA256['aarch64']};; "
        '*) echo "fx: unsupported architecture $(uname -m)" >&2; false;; esac && '
        'd="$(mktemp -d)" && '
        f'curl -fsSL -o "$d/fx.tar.gz" "{FX_RELEASE_URL}/fx-linux-$a.tar.gz" && '
        'echo "$h  $d/fx.tar.gz" | sha256sum -c - >/dev/null && '
        'tar -xzf "$d/fx.tar.gz" -C "$d" && '
        'mkdir -p "$HOME/.local/bin" && mv "$d/fx" "$HOME/.local/bin/fx" && chmod +x "$HOME/.local/bin/fx"; '
        "} && fx --version"
    )


def _fx_settings(gateway: GatewayConfig, reasoning_effort: str | None) -> dict[str, object]:
    """Build fx's ``settings.json`` with one custom provider that is the model gateway.

    Args:
        gateway: Gateway URL and key.
        reasoning_effort: The user's effort for the model, or ``None`` for fx's default.

    Returns:
        The settings content.

    Raises:
        ServiceError: When an effort is set. fx 0.0.16 sends no effort through
            a custom ``openai-chat-completions`` provider: the provider's model
            metadata cannot list effort levels, so fx drops a saved ``effort``,
            and its chat-completions request check rejects a reasoning option.
    """
    if reasoning_effort:
        raise ServiceError(
            f"The fx harness does not support the reasoning effort '{reasoning_effort}'. "
            "fx cannot send an effort through the Skynet gateway; choose no effort."
        )
    settings: dict[str, object] = {
        "providers": {
            FX_PROVIDER: {
                "protocol": "openai-chat-completions",
                "base_url": gateway.url,
                "auth": {"type": "bearer", "env": ENV_API_KEY},
            }
        }
    }
    return settings


def _fx_launch(model: str, gateway: GatewayConfig, *, reasoning_effort: str | None = None) -> HarnessLaunch:
    """Build the launch for fx (``fx``, pinned to :data:`FX_VERSION`).

    fx writes its ``--json`` result to a file, and :data:`FX_TO_PI` prints
    it as a Pi line, so the bridge reads the answer and usage with its
    Pi parser. The run keeps fx's exit status.

    Args:
        model: Target model id.
        gateway: Gateway URL and key.
        reasoning_effort: The user's effort for the model, or ``None`` for fx's default.

    Returns:
        The launch.

    Raises:
        ServiceError: When an effort is set; see :func:`_fx_settings`.
    """
    env = _base_env(model, gateway)
    env.update({"FX_MODEL": model, "FX_PROVIDER": FX_PROVIDER, "FX_AUTO_UPGRADE": "0", "FX_SOUND": "0"})
    return HarnessLaunch(
        instructions_file="AGENTS.md",
        install_command=_fx_install(),
        files={FX_SETTINGS_FILE: json.dumps(_fx_settings(gateway, reasoning_effort), indent=2)},
        run_command=(
            f'{_FX_PATH}; mkdir -p "$HOME/.fx" && cp {FX_SETTINGS_FILE} "$HOME/.fx/settings.json" && '
            f'fx ask --yolo --json -- {_PROMPT_ARG} < /dev/null > "$HOME/.fx/ask.json"; fx_status=$?; '
            f"python3 -I -c '{FX_TO_PI}' \"$HOME/.fx/ask.json\"; (exit $fx_status)"
        ),
        env=env,
        parse_output=parse_pi_output,
    )


SPEC = register(
    HarnessSpec(
        id=BLACKBOX_HARNESS_FX,
        launch=_fx_launch,
        binary="fx",
        version=FX_VERSION,
        output_format="pi",
        protected_env=_PROTECTED_ENV,
        position=62,
    )
)
