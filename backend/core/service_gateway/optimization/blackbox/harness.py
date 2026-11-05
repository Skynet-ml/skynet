"""Harness catalog: how each coding agent is installed, pointed at the gateway and run.

A harness is the program that turns a model into a coding agent — Pi,
Codex, OpenCode, Prime Agent, or a user-supplied command. Every entry
knows the file its instructions live in (that file is the text under
optimization), how to install itself, the config that routes its model
calls to Skynet's gateway, the command that runs one case headlessly, and
how to read the agent's final answer and token usage out of its output.
Model routing is gateway-only: a sandbox sees the run's gateway key, never
a provider key.
"""

from __future__ import annotations

import json
import shlex
from collections.abc import Callable
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

from ....config import settings, with_legacy_env
from ....exceptions import ServiceError
from ....models.blackbox import (
    BLACKBOX_HARNESS_CODEX,
    BLACKBOX_HARNESS_CUSTOM,
    BLACKBOX_HARNESS_OPENCODE,
    BLACKBOX_HARNESS_PI,
    BLACKBOX_HARNESS_PRIME,
    BlackboxTarget,
)
from ....worker import host_agent_shim
from .harness_bridge import (
    PARSERS,
    parse_codex_output,
    parse_host_agent_output,
    parse_opencode_output,
    parse_pi_output,
    parse_plain_output,
)

PROVIDER = "app"
PROMPT_FILE = "task/PROMPT.md"
ANSWER_FILE = "output/answer.txt"
ENV_MODEL = "APP_MODEL"
ENV_GATEWAY_URL = "APP_GATEWAY_URL"
ENV_API_KEY = "APP_API_KEY"
ENV_PROMPT_FILE = "APP_PROMPT_FILE"
ENV_ANSWER_FILE = "APP_ANSWER_FILE"
# Set for a custom harness only when the run chose an effort, so its command
# can read the level without it ever being spliced into the command text.
ENV_REASONING_EFFORT = "APP_REASONING_EFFORT"
# Substituted verbatim into a custom harness's commands; the env vars above
# are the quoting-safe alternative. Saved custom commands may still use the
# ``SKYNET_*`` spellings, so every launch sets both.
PLACEHOLDERS = ("{model}", "{gateway_url}", "{api_key}", "{prompt_file}", "{answer_file}")

Usage = dict[str, int]
OutputParser = Callable[[str], tuple[str | None, Usage]]
# The parser a launch names must also exist inside the sandbox, where the
# bridge replays it without the backend on the path; the bridge owns the table.
_PARSER_NAMES = {parser: name for name, parser in PARSERS.items()}

_PROMPT_ARG = f'"$(cat "${ENV_PROMPT_FILE}")"'


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


PI_PACKAGE = "@earendil-works/pi-coding-agent"
PI_VERSION = "0.87.1"
CODEX_VERSION = "0.153.0"
OPENCODE_VERSION = "1.18.27"
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
_PINNED_HARNESSES = {
    BLACKBOX_HARNESS_PI: ("pi", PI_VERSION),
    BLACKBOX_HARNESS_CODEX: ("codex", CODEX_VERSION),
    BLACKBOX_HARNESS_OPENCODE: ("opencode", OPENCODE_VERSION),
    BLACKBOX_HARNESS_PRIME: ("prime-agent", PRIME_AGENT_VERSION),
}
PI_THINKING_LEVEL = "low"
# Skynet's effort names that the CLIs spell differently; others pass through.
_PI_THINKING_LEVELS = {"none": "off"}
# The LiteLLM-style routing prefix; the gateway fronts OpenRouter itself and
# rejects model ids that still carry it.
_OPENROUTER_PREFIX = "openrouter/"
# The sandbox image ships its own pi in /usr/local/bin. A pinned copy lives in
# a private prefix put ahead of it on PATH, so the version the transcript logs
# is the one that ran.
_PI_PREFIX = '"$HOME/.app/pi"'
_PI_PATH = 'export PATH="$HOME/.app/pi/bin:$PATH"'


def _pi_install() -> str:
    """Return the command that pins pi to :data:`PI_VERSION` and logs the version that will run.

    The image's own pi is kept when it already is the pinned version, so the
    install is free on the common path.

    Returns:
        The shell command.
    """
    return (
        f'{_PI_PATH}; [ "$(pi --version 2>/dev/null)" = "{PI_VERSION}" ] '
        f"|| npm install -g --prefix {_PI_PREFIX} {PI_PACKAGE}@{PI_VERSION} >/dev/null && pi --version"
    )


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


def _with_relay_config(command: str, files: dict[str, str], gateway: GatewayConfig) -> str:
    """Point only generated runtime configuration at the sandbox-local mailbox proxy.

    Args:
        command: Original built-in harness command.
        files: Fixed generated harness configuration files.
        gateway: Parent endpoint replaced only when its guest-local relay is present.

    Returns:
        The original command preceded by a scoped endpoint substitution.
    """
    script = (
        "import os,pathlib; "
        "endpoint=os.environ.get('APP_BUDGET_RELAY_URL') or os.environ.get('SKYNET_BUDGET_RELAY_URL'); "
        f"files={list(files)!r}; old={gateway.url!r}; "
        "[(lambda p: p.write_text(p.read_text().replace(old,endpoint)))(pathlib.Path(name)) "
        "for name in files if endpoint]"
    )
    return f"python3 -c {shlex.quote(script)} && {command}"


def pinned_harness_check(harness: str) -> str | None:
    """Return the offline exact-version probe for a built-in harness.

    Args:
        harness: Selected built-in or custom harness identifier.

    Returns:
        A CLI metadata command, or None for a custom command without a pinned executable.
    """
    selected = _PINNED_HARNESSES.get(harness)
    return _version_check(*selected) if selected else None


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


def _pi_models_json(
    model: str, gateway: GatewayConfig, *, reasoning_effort: str | None = None
) -> tuple[dict[str, Any], str]:
    """Build Pi's ``models.json`` for the gateway and the ``--model`` argument that selects the entry.

    Prime Agent is a Pi fork and reads the same file.

    Args:
        model: Target model id.
        gateway: Gateway URL and key.
        reasoning_effort: The user's effort for the model, or ``None`` for the default.

    Returns:
        The ``models.json`` content and the quoted ``--model`` argument.
    """
    openrouter = "openrouter.ai" in gateway.url
    # An explicit effort comes from a model the catalog lists as reasoning-capable,
    # so its gateway accepts the field the default leaves out.
    reasoning = openrouter or bool(reasoning_effort)
    level = _PI_THINKING_LEVELS.get(reasoning_effort, reasoning_effort) if reasoning_effort else PI_THINKING_LEVEL
    entry: dict[str, object] = {
        "id": model,
        "name": model,
        # OpenRouter models such as DeepSeek reason by default and, left to that default, think until
        # they hit ``maxTokens`` without a single tool call or answer. Pi's OpenRouter thinking format
        # makes every request carry an explicit effort instead; other gateways may reject that field.
        "reasoning": reasoning,
        "input": ["text"],
        "contextWindow": 200_000,
        "maxTokens": 32_000,
        "cost": {"input": 0, "output": 0, "cacheRead": 0, "cacheWrite": 0},
    }
    if openrouter:
        entry["compat"] = {"thinkingFormat": "openrouter"}
    if reasoning_effort:
        # Without a map entry Pi sends no effort for ``off`` on an OpenAI-style gateway, so the model
        # keeps its default thinking, and it clamps ``xhigh`` and ``max`` down to ``high``.
        entry["thinkingLevelMap"] = {level: reasoning_effort}
    model_arg = f'"${ENV_MODEL}:{level}"' if reasoning else f'"${ENV_MODEL}"'
    models_json = {
        "providers": {
            PROVIDER: {
                "baseUrl": gateway.url,
                "api": "openai-completions",
                # Pi reads a bare env-var name as the literal key; the shell form is honoured.
                "apiKey": f"!printenv {ENV_API_KEY}",
                "models": [entry],
            }
        }
    }
    return models_json, model_arg


def _pi_launch(model: str, gateway: GatewayConfig, *, reasoning_effort: str | None = None) -> HarnessLaunch:
    """Build the launch for Pi (:data:`PI_PACKAGE`, pinned to :data:`PI_VERSION`).

    Args:
        model: Target model id.
        gateway: Gateway URL and key.
        reasoning_effort: The user's effort for the model, or ``None`` for the default.

    Returns:
        The launch.
    """
    models_json, model_arg = _pi_models_json(model, gateway, reasoning_effort=reasoning_effort)
    return HarnessLaunch(
        instructions_file="AGENTS.md",
        install_command=_pi_install(),
        files={".app/pi/models.json": json.dumps(models_json, indent=2)},
        run_command=(
            f'{_PI_PATH}; mkdir -p "$HOME/.pi/agent" && cp .app/pi/models.json "$HOME/.pi/agent/models.json" && '
            f"pi --mode json --no-session --provider {PROVIDER} --model {model_arg} {_PROMPT_ARG}"
        ),
        env=_base_env(model, gateway),
        parse_output=parse_pi_output,
    )


def _codex_launch(model: str, gateway: GatewayConfig, *, reasoning_effort: str | None = None) -> HarnessLaunch:
    """Build the launch for Codex CLI (``@openai/codex``).

    Args:
        model: Target model id.
        gateway: Gateway URL and key.
        reasoning_effort: The user's effort for the model, or ``None`` for Codex's default.

    Returns:
        The launch.
    """
    config_toml = (
        f"model = {json.dumps(model)}\n"
        f'model_provider = "{PROVIDER}"\n'
        + (f"model_reasoning_effort = {json.dumps(reasoning_effort)}\n" if reasoning_effort else "")
        + "\n"
        f"[model_providers.{PROVIDER}]\n"
        'name = "Model gateway"\n'
        f"base_url = {json.dumps(gateway.url)}\n"
        f'env_key = "{ENV_API_KEY}"\n'
        'wire_api = "responses"\n'
        "request_max_retries = 0\n"
        "stream_max_retries = 0\n"
        "supports_websockets = false\n"
    )
    return HarnessLaunch(
        instructions_file="AGENTS.md",
        install_command=_npm_install("codex", "@openai/codex", CODEX_VERSION),
        files={".app/codex/config.toml": config_toml},
        run_command=(
            'CODEX_HOME="$PWD/.app/codex" codex exec --skip-git-repo-check '
            f"--dangerously-bypass-approvals-and-sandbox --json {_PROMPT_ARG}"
        ),
        env=_base_env(model, gateway),
        parse_output=parse_codex_output,
    )


def _opencode_launch(model: str, gateway: GatewayConfig, *, reasoning_effort: str | None = None) -> HarnessLaunch:
    """Build the launch for OpenCode (``opencode-ai``).

    Args:
        model: Target model id.
        gateway: Gateway URL and key.
        reasoning_effort: The user's effort for the model, or ``None`` for OpenCode's default.

    Returns:
        The launch.
    """
    entry: dict[str, object] = {"name": model}
    if reasoning_effort:
        # OpenCode hands model options to its bundled @ai-sdk/openai-compatible as
        # provider options, which sends this one as ``reasoning_effort``.
        entry["options"] = {"reasoningEffort": reasoning_effort}
    config = {
        "$schema": "https://opencode.ai/config.json",
        "provider": {
            PROVIDER: {
                "npm": "@ai-sdk/openai-compatible",
                "name": "Model gateway",
                "options": {"baseURL": gateway.url, "apiKey": f"{{env:{ENV_API_KEY}}}"},
                "models": {model: entry},
            }
        },
        "model": f"{PROVIDER}/{model}",
    }
    return HarnessLaunch(
        instructions_file="AGENTS.md",
        install_command=_npm_install("opencode", "opencode-ai", OPENCODE_VERSION),
        files={"opencode.json": json.dumps(config, indent=2)},
        run_command=f'opencode run --format json --model "{PROVIDER}/${ENV_MODEL}" {_PROMPT_ARG}',
        env=_base_env(model, gateway),
        parse_output=parse_opencode_output,
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
            f"prime-agent --mode json --no-session --provider {PROVIDER} --model {model_arg} {_PROMPT_ARG}"
        ),
        env=env,
        parse_output=parse_pi_output,
    )


# The tools a ShinkaEvolve agent-editor session gets: it reads, searches and
# edits the version's files, and can run nothing.
PI_EDITOR_TOOLS = ("read", "grep", "find", "ls", "edit", "write")
# Each editor session reads its own config from its workspace, so concurrent
# sessions on different model routes never share one ``~/.pi/agent``.
PI_EDITOR_CONFIG_DIR = ".app/pi"


def pi_editor_launch(
    model: str,
    gateway: GatewayConfig,
    *,
    headers: dict[str, str] | None = None,
    protected: bool = False,
    reasoning_effort: str | None = None,
) -> HarnessLaunch:
    """Build the Pi launch a ShinkaEvolve agent-editor session runs.

    It is Pi's catalog launch narrowed to file tools, with every extension,
    skill and context file switched off so the prompt is all it sees.

    Args:
        model: Model id the route serves.
        gateway: Route URL and key the session calls.
        headers: Extra headers every model request carries (the usage tags).
        protected: Require the image's pinned Pi and the sandbox-local relay.
        reasoning_effort: The user's effort for the model, or ``None`` for the default.

    Returns:
        The launch.
    """
    model = model.removeprefix(_OPENROUTER_PREFIX)
    models_json, model_arg = _pi_models_json(model, gateway, reasoning_effort=reasoning_effort)
    if headers:
        models_json["providers"][PROVIDER]["headers"] = dict(headers)
    files = {f"{PI_EDITOR_CONFIG_DIR}/models.json": json.dumps(models_json, indent=2)}
    # The prompt can carry whole files of a repository version; an ``@file``
    # attachment avoids the kernel's per-argument size limit that inlining hits.
    run_command = (
        f'{_PI_PATH}; PI_CODING_AGENT_DIR="$PWD/{PI_EDITOR_CONFIG_DIR}" pi --mode json --no-session '
        f"--no-extensions --no-skills --no-context-files --tools {','.join(PI_EDITOR_TOOLS)} "
        f'--provider {PROVIDER} --model {model_arg} @"${ENV_PROMPT_FILE}" "Do the task in the attached file."'
    )
    if protected:
        run_command = _with_relay_config(run_command, files, gateway)
    return HarnessLaunch(
        instructions_file="AGENTS.md",
        install_command=pinned_harness_check(BLACKBOX_HARNESS_PI) if protected else _pi_install(),
        files=files,
        run_command=run_command,
        env=_base_env(model, gateway),
        parse_output=parse_pi_output,
    )


HOST_AGENT_SHIM_FILE = ".app/host-agent/shim.py"


def host_cli_launch(model: str, gateway: GatewayConfig, host_cli: str, host_agent_token: str | None) -> HarnessLaunch:
    """Build the launch that hands each proposer turn to a CLI on the user's machine.

    The sandbox runs only the stdlib shim; the parent runs the CLI on a copy
    of the workspace (:mod:`core.billing.host_agent`). The parent picks the CLI
    and its permission from the run settings, so the shim sends neither.

    Args:
        model: Proposer model id, passed through to the parent.
        gateway: Gateway URL the shim reaches the parent through.
        host_cli: ``codex`` or ``claude``.
        host_agent_token: The token the parent minted for this proposer.

    Returns:
        The launch.

    Raises:
        ServiceError: When local mode is off, the CLI is unknown, or the token is missing.
    """
    if not settings.local_mode:
        raise ServiceError("Host CLI proposers run only in the Mac app.")
    instructions = host_agent_shim.INSTRUCTIONS_FILES.get(host_cli)
    if instructions is None:
        raise ServiceError(f"Unknown host CLI '{host_cli}'.")
    if not host_agent_token:
        raise ServiceError("This run has no host proposer token.")
    env = _base_env(model, gateway)
    env.update(
        with_legacy_env(
            {
                "APP_HOST_AGENT_TOKEN": host_agent_token,
                "APP_HOST_AGENT_TIMEOUT": str(settings.host_agent_timeout_seconds),
            }
        )
    )
    return HarnessLaunch(
        instructions_file=instructions,
        run_command=f'python3 "$PWD/{HOST_AGENT_SHIM_FILE}"',
        files={HOST_AGENT_SHIM_FILE: Path(host_agent_shim.__file__).read_text(encoding="utf-8")},
        env=env,
        parse_output=parse_host_agent_output,
    )


def _fill(command: str | None, model: str, gateway: GatewayConfig, *, protected: bool = False) -> str | None:
    """Substitute the custom-harness placeholders into ``command``.

    Args:
        command: A command line that may use :data:`PLACEHOLDERS`.
        model: Target model id.
        gateway: Gateway URL and key.
        protected: Resolve gateway placeholders from the sandbox-local relay environment.

    Returns:
        The command with placeholders replaced, or ``None`` when there is no command.
    """
    if not command or not command.strip():
        return None
    values = {
        "{model}": model,
        "{gateway_url}": f"${ENV_GATEWAY_URL}" if protected else gateway.url,
        "{api_key}": gateway.api_key,
        "{prompt_file}": PROMPT_FILE,
        "{answer_file}": ANSWER_FILE,
    }
    for placeholder, value in values.items():
        command = command.replace(placeholder, value)
    return command


_CATALOG: dict[str, Callable[..., HarnessLaunch]] = {
    BLACKBOX_HARNESS_PI: _pi_launch,
    BLACKBOX_HARNESS_CODEX: _codex_launch,
    BLACKBOX_HARNESS_OPENCODE: _opencode_launch,
    BLACKBOX_HARNESS_PRIME: _prime_agent_launch,
}


def build_launch(
    target: BlackboxTarget,
    gateway: GatewayConfig,
    *,
    protected: bool = False,
    host_cli: str | None = None,
    host_agent_token: str | None = None,
    reasoning_effort: str | None = None,
) -> HarnessLaunch:
    """Resolve the launch for a job's target: catalog harness or custom command.

    ``target.install_command`` and ``target.run_command`` override the
    catalog defaults when set; a custom harness is nothing but those two.
    A ``host_cli`` replaces both with the host-agent shim (local mode only).

    Args:
        target: The job's agent target.
        gateway: Gateway URL and key the harness routes its model calls through.
        protected: Require preinstalled pinned dependencies and the isolated model mailbox.
        host_cli: The user's own CLI that runs each proposer turn, if any.
        host_agent_token: The token the parent minted for that host proposer.
        reasoning_effort: The proposer model's effort level. Catalog harnesses apply it;
            a custom harness reads it from :data:`ENV_REASONING_EFFORT`.

    Returns:
        The launch.

    Raises:
        ServiceError: When the harness id is unknown.
    """
    model = str(target.model).removeprefix(_OPENROUTER_PREFIX)
    if host_cli is not None:
        return host_cli_launch(model, gateway, host_cli, host_agent_token)
    if target.harness == BLACKBOX_HARNESS_CUSTOM:
        run_command = _fill(target.run_command, model, gateway, protected=protected)
        if run_command is None:
            raise ServiceError("A custom harness needs a run_command.")
        env = _base_env(model, gateway)
        if reasoning_effort:
            env.update(with_legacy_env({ENV_REASONING_EFFORT: reasoning_effort}))
        return HarnessLaunch(
            instructions_file="AGENTS.md",
            install_command=_fill(target.install_command, model, gateway, protected=protected),
            run_command=run_command,
            env=env,
            parse_output=parse_plain_output,
        )
    factory = _CATALOG.get(target.harness)
    if factory is None:
        raise ServiceError(f"Unknown harness '{target.harness}'.")
    launch = factory(model, gateway, reasoning_effort=reasoning_effort)
    install_command = _fill(target.install_command, model, gateway, protected=protected) or launch.install_command
    run_command = _fill(target.run_command, model, gateway, protected=protected) or launch.run_command
    env = dict(launch.env)
    if protected and not target.install_command:
        install_command = pinned_harness_check(target.harness)
    if protected and launch.files:
        run_command = _with_relay_config(run_command, launch.files, gateway)
    if protected and target.harness == BLACKBOX_HARNESS_OPENCODE:
        env.update(
            {
                "OPENCODE_DISABLE_AUTOUPDATE": "true",
                "OPENCODE_DISABLE_MODELS_FETCH": "true",
                "OPENCODE_DISABLE_DEFAULT_PLUGINS": "true",
                "OPENCODE_DISABLE_LSP_DOWNLOAD": "true",
            }
        )
    return HarnessLaunch(
        instructions_file=launch.instructions_file,
        install_command=install_command,
        run_command=run_command,
        files=launch.files,
        env=env,
        parse_output=launch.parse_output,
    )


def launch_payload(launch: HarnessLaunch) -> dict[str, Any]:
    """Serialize a launch so the sandbox bridge can replay it without Skynet code.

    Args:
        launch: Fully filled harness launch.

    Returns:
        JSON-ready launch description keyed the way ``harness_bridge`` reads it.
    """
    return {
        "instructions_file": launch.instructions_file,
        "run_command": launch.run_command,
        "install_command": launch.install_command,
        "files": dict(launch.files),
        "env": dict(launch.env),
        "output_format": _PARSER_NAMES[launch.parse_output],
    }
