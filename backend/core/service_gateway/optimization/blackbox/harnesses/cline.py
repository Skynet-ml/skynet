"""Cline CLI harness (``cline``).

Adapted from Harbor 0.25.0 (Apache-2.0), agents/installed/cline: the npm
install, the seeded ``globalState.json`` that skips the welcome flow, and the
``-P``/``-m``/``--yolo`` one-shot run with the prompt after ``--``. Harbor
never gives Cline a base URL; Skynet pins the release and seeds Cline's
OpenAI-compatible provider with the model gateway URL and token instead.
"""

from __future__ import annotations

import shlex

from .....exceptions import ServiceError
from .....models.blackbox import BLACKBOX_HARNESS_CLINE
from ..harness_bridge import parse_plain_output
from . import HarnessSpec, register
from .base import _PROMPT_ARG, ENV_API_KEY, ENV_MODEL, GatewayConfig, HarnessLaunch, _base_env, _npm_install

# npm registry ``dist-tags.latest`` for ``cline`` on 2026-11-08 (Apache-2.0).
CLINE_VERSION = "3.0.74"
# Cline's "OpenAI Compatible" provider; it is the one that takes ``--baseurl``.
CLINE_PROVIDER = "openai"
# The gateway URL lives in this file rather than on the command line, so a
# protected run's relay rewrite of generated files reaches it.
CLINE_BASE_URL_FILE = ".app/cline/base_url"
_GLOBAL_STATE = '{"welcomeViewCompleted": true, "isNewUser": false}'
# The ``--thinking`` levels of the pinned release (its README options table).
# Skynet's ``minimal`` and ``max`` are other levels, not other spellings, so
# Cline refuses them instead of running at a level the user did not pick.
CLINE_THINKING_LEVELS = ("none", "low", "medium", "high", "xhigh")
# Cline reads these keys for its other providers. A protected run blanks them,
# so an inherited key never gives Cline a route around the gateway.
_PROTECTED_ENV = {
    "ANTHROPIC_API_KEY": "",
    "CLINE_API_KEY": "",
    "OPENROUTER_API_KEY": "",
    "AI_GATEWAY_API_KEY": "",
    "V0_API_KEY": "",
    # Cline documents no telemetry or update switch; this is the generic opt-out.
    "DO_NOT_TRACK": "1",
}


def _cline_thinking(reasoning_effort: str | None) -> str:
    """Return the ``--thinking`` flag for the user's effort, or nothing for Cline's default.

    Args:
        reasoning_effort: The user's effort for the model, or ``None``.

    Returns:
        The flag with a trailing space, or an empty string.

    Raises:
        ServiceError: When Cline does not support the effort level.
    """
    if not reasoning_effort:
        return ""
    if reasoning_effort not in CLINE_THINKING_LEVELS:
        raise ServiceError(
            f"Cline does not support the effort level '{reasoning_effort}'. "
            f"Supported levels: {', '.join(CLINE_THINKING_LEVELS)}."
        )
    return f"--thinking {reasoning_effort} "


def _cline_launch(model: str, gateway: GatewayConfig, *, reasoning_effort: str | None = None) -> HarnessLaunch:
    """Build the launch for Cline CLI (``cline``, pinned to :data:`CLINE_VERSION`).

    Each run gets a fresh Cline data directory outside the workspace, so the
    saved gateway token never lands in the files the run hands back. Cline's
    plain one-shot output is the answer; the bridge has no parser for its
    NDJSON stream.

    Args:
        model: Target model id.
        gateway: Gateway URL and key.
        reasoning_effort: The user's effort for the model, or ``None`` for Cline's default.

    Returns:
        The launch.

    Raises:
        ServiceError: When Cline does not support the effort level.
    """
    key = f'"${ENV_API_KEY}"'
    model_arg = f'"${ENV_MODEL}"'
    run_command = (
        'export CLINE_DATA_DIR="$(mktemp -d)" && '
        f'printf %s {shlex.quote(_GLOBAL_STATE)} > "$CLINE_DATA_DIR/globalState.json" && '
        f"cline auth --provider {CLINE_PROVIDER} --apikey {key} --modelid {model_arg} "
        f'--baseurl "$(cat {CLINE_BASE_URL_FILE})" >/dev/null && '
        f"OPENAI_API_KEY={key} cline -P {CLINE_PROVIDER} -k {key} -m {model_arg} --yolo "
        f"{_cline_thinking(reasoning_effort)}-- {_PROMPT_ARG} < /dev/null"
    )
    return HarnessLaunch(
        instructions_file="AGENTS.md",
        install_command=_npm_install("cline", "cline", CLINE_VERSION),
        files={CLINE_BASE_URL_FILE: gateway.url},
        run_command=run_command,
        env=_base_env(model, gateway),
        parse_output=parse_plain_output,
    )


SPEC = register(
    HarnessSpec(
        id=BLACKBOX_HARNESS_CLINE,
        launch=_cline_launch,
        binary="cline",
        version=CLINE_VERSION,
        output_format="plain",
        protected_env=_PROTECTED_ENV,
        position=70,
        supported_efforts=CLINE_THINKING_LEVELS,
    )
)
