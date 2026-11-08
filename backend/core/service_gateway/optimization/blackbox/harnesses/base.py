"""Shared types and helpers every harness module builds its launch from."""

from __future__ import annotations

import shlex
from collections.abc import Callable
from dataclasses import dataclass, field

from .....config import with_legacy_env

PROVIDER = "app"
PROMPT_FILE = "task/PROMPT.md"
ANSWER_FILE = "output/answer.txt"
ENV_MODEL = "APP_MODEL"
ENV_GATEWAY_URL = "APP_GATEWAY_URL"
ENV_API_KEY = "APP_API_KEY"
ENV_PROMPT_FILE = "APP_PROMPT_FILE"
ENV_ANSWER_FILE = "APP_ANSWER_FILE"

Usage = dict[str, int]
OutputParser = Callable[[str], tuple[str | None, Usage]]

_PROMPT_ARG = f'"$(cat "${ENV_PROMPT_FILE}")"'
# The bridge sets these only when the run hides scoring-side paths: Pi and
# Prime load the refusal extension even with --no-extensions, and Codex reads
# the refusal hook from its home directory.
_GUARD_PI_ARG = '${APP_GUARD_PI:+-e "$APP_GUARD_PI"} '
_GUARD_CODEX_COPY = '[ -z "$APP_GUARD_CODEX" ] || cp "$APP_GUARD_CODEX" .app/codex/hooks.json; '


@dataclass(frozen=True)
class GatewayConfig:
    """Where a sandboxed harness sends its model calls."""

    url: str
    api_key: str


@dataclass(frozen=True)
class HarnessLaunch:
    """Everything a sandbox needs to run one harness on one case."""

    instructions_file: str
    run_command: str
    parse_output: OutputParser
    install_command: str | None = None
    files: dict[str, str] = field(default_factory=dict)
    env: dict[str, str] = field(default_factory=dict)


def _npm_install(binary: str, package: str, version: str) -> str:
    """Return a command that installs ``package`` globally unless ``binary`` is already on PATH.

    Args:
        binary: Executable the package provides.
        package: npm package name.
        version: Exact release required by the runtime profile.

    Returns:
        The shell command.
    """
    return f"{_version_check(binary, version)} || npm install -g {package}@{version}"


def _version_check(binary: str, version: str) -> str:
    """Check the required CLI release without installing packages or invoking a model.

    Args:
        binary: Built-in harness executable name.
        version: Exact release recorded in the deployment lockfile.

    Returns:
        A shell command that fails when the pinned executable is unavailable.
    """
    pattern = "(^| )" + version.replace(".", "[.]") + "( |$)"
    return f"{binary} --version 2>/dev/null | grep -Eq {shlex.quote(pattern)}"


def _base_env(model: str, gateway: GatewayConfig) -> dict[str, str]:
    """Return the environment every harness gets.

    Args:
        model: Target model id as the gateway knows it.
        gateway: Gateway URL and key.

    Returns:
        Environment variables for the sandbox.
    """
    return with_legacy_env(
        {
            ENV_MODEL: model,
            ENV_GATEWAY_URL: gateway.url,
            ENV_API_KEY: gateway.api_key,
            ENV_PROMPT_FILE: PROMPT_FILE,
            ENV_ANSWER_FILE: ANSWER_FILE,
            "CI": "1",
            "NO_COLOR": "1",
        }
    )
