"""Harness catalog: how each coding agent is installed, pointed at the gateway and run.

A harness is the program that turns a model into a coding agent — Pi,
Codex, OpenCode, Prime Agent, or a user-supplied command. Every entry
knows the file its instructions live in (that file is the text under
optimization), how to install itself, the config that routes its model
calls to Skynet's gateway, the command that runs one case headlessly, and
how to read the agent's final answer and token usage out of its output.
Model routing is gateway-only: a sandbox sees the run's gateway key, never
a provider key. Each built-in CLI lives in its own :mod:`.harnesses` module;
this module keeps the shared launch logic and re-exports the old names.
"""

from __future__ import annotations

import json
import shlex
from collections.abc import Callable, Iterator, Mapping
from pathlib import Path
from typing import Any

from ....config import settings, with_legacy_env
from ....exceptions import ServiceError
from ....models.blackbox import BLACKBOX_HARNESS_CUSTOM, BLACKBOX_HARNESS_PI, BlackboxTarget
from ....worker import host_agent_shim
from .harness_bridge import PARSERS, parse_host_agent_output, parse_pi_output, parse_plain_output
from .harnesses import REGISTRY, HarnessSpec
from .harnesses.base import _GUARD_CODEX_COPY as _GUARD_CODEX_COPY
from .harnesses.base import _GUARD_PI_ARG as _GUARD_PI_ARG
from .harnesses.base import _PROMPT_ARG as _PROMPT_ARG
from .harnesses.base import ANSWER_FILE as ANSWER_FILE
from .harnesses.base import ENV_ANSWER_FILE as ENV_ANSWER_FILE
from .harnesses.base import ENV_API_KEY as ENV_API_KEY
from .harnesses.base import ENV_GATEWAY_URL as ENV_GATEWAY_URL
from .harnesses.base import ENV_MODEL as ENV_MODEL
from .harnesses.base import ENV_PROMPT_FILE as ENV_PROMPT_FILE
from .harnesses.base import PROMPT_FILE as PROMPT_FILE
from .harnesses.base import PROVIDER as PROVIDER
from .harnesses.base import GatewayConfig as GatewayConfig
from .harnesses.base import HarnessLaunch as HarnessLaunch
from .harnesses.base import OutputParser as OutputParser
from .harnesses.base import Usage as Usage
from .harnesses.base import _base_env as _base_env
from .harnesses.base import _npm_install as _npm_install
from .harnesses.base import _version_check as _version_check
from .harnesses.codex import CODEX_VERSION as CODEX_VERSION
from .harnesses.codex import _codex_launch as _codex_launch
from .harnesses.opencode import OPENCODE_VERSION as OPENCODE_VERSION
from .harnesses.opencode import _opencode_launch as _opencode_launch
from .harnesses.pi import _PI_PATH as _PI_PATH
from .harnesses.pi import _PI_PREFIX as _PI_PREFIX
from .harnesses.pi import _PI_THINKING_LEVELS as _PI_THINKING_LEVELS
from .harnesses.pi import PI_PACKAGE as PI_PACKAGE
from .harnesses.pi import PI_THINKING_LEVEL as PI_THINKING_LEVEL
from .harnesses.pi import PI_VERSION as PI_VERSION
from .harnesses.pi import _pi_install as _pi_install
from .harnesses.pi import _pi_launch as _pi_launch
from .harnesses.pi import _pi_models_json as _pi_models_json
from .harnesses.prime import _PRIME_AGENT_DIR as _PRIME_AGENT_DIR
from .harnesses.prime import PRIME_AGENT_IMAGE_KERNEL as PRIME_AGENT_IMAGE_KERNEL
from .harnesses.prime import PRIME_AGENT_TARBALL as PRIME_AGENT_TARBALL
from .harnesses.prime import PRIME_AGENT_VERSION as PRIME_AGENT_VERSION
from .harnesses.prime import _prime_agent_install as _prime_agent_install
from .harnesses.prime import _prime_agent_launch as _prime_agent_launch

# Set for a custom harness only when the run chose an effort, so its command
# can read the level without it ever being spliced into the command text.
ENV_REASONING_EFFORT = "APP_REASONING_EFFORT"
# Substituted verbatim into a custom harness's commands; the env vars above
# are the quoting-safe alternative. Saved custom commands may still use the
# ``SKYNET_*`` spellings, so every launch sets both.
PLACEHOLDERS = ("{model}", "{gateway_url}", "{api_key}", "{prompt_file}", "{answer_file}")

# The parser a launch names must also exist inside the sandbox, where the
# bridge replays it without the backend on the path; the bridge owns the table.
_PARSER_NAMES = {parser: name for name, parser in PARSERS.items()}

# The LiteLLM-style routing prefix; the gateway fronts OpenRouter itself and
# rejects model ids that still carry it.
_OPENROUTER_PREFIX = "openrouter/"


class _RegistryView(Mapping[str, Any]):
    """A live read-only projection of :data:`REGISTRY`, so a newly registered harness appears in it."""

    def __init__(self, project: Callable[[HarnessSpec], Any]) -> None:
        """Store the projection applied to each spec.

        Args:
            project: Maps a spec to the value this view exposes for it.
        """
        self._project = project

    def __getitem__(self, harness: str) -> Any:
        """Return the projected value for one harness id.

        Args:
            harness: Registered harness id.

        Returns:
            The projected value.

        Raises:
            KeyError: When the harness is not registered.
        """
        return self._project(REGISTRY[harness])

    def __iter__(self) -> Iterator[str]:
        """Iterate the registered harness ids in registry order.

        Returns:
            An iterator over the ids.
        """
        return iter(REGISTRY)

    def __len__(self) -> int:
        """Return the number of registered harnesses.

        Returns:
            The count.
        """
        return len(REGISTRY)


_CATALOG: Mapping[str, Callable[..., HarnessLaunch]] = _RegistryView(lambda spec: spec.launch)
_PINNED_HARNESSES: Mapping[str, tuple[str, str]] = _RegistryView(lambda spec: (spec.binary, spec.version))


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
        f'--provider {PROVIDER} --model {model_arg} {_GUARD_PI_ARG}@"${ENV_PROMPT_FILE}" "Do the task in the attached file."'
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
    spec = REGISTRY.get(target.harness)
    if spec is None:
        raise ServiceError(f"Unknown harness '{target.harness}'.")
    launch = spec.launch(model, gateway, reasoning_effort=reasoning_effort)
    install_command = _fill(target.install_command, model, gateway, protected=protected) or launch.install_command
    run_command = _fill(target.run_command, model, gateway, protected=protected) or launch.run_command
    env = dict(launch.env)
    if protected and not target.install_command:
        install_command = pinned_harness_check(target.harness)
    if protected and launch.files:
        run_command = _with_relay_config(run_command, launch.files, gateway)
    if protected:
        env.update(spec.protected_env)
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
