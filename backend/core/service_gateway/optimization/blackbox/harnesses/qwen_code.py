"""Qwen Code CLI harness (``qwen``).

Adapted from Harbor 0.25.0 (Apache-2.0), agents/installed/qwen_code: the npm
install of ``@qwen-code/qwen-code`` and the ``--yolo --auth-type openai``
one-shot run with ``--prompt``. Harbor installs through nvm, passes a direct
provider key with ``--openai-api-key`` and records the chat under
``~/.qwen``; Skynet pins one release, gives Qwen Code the gateway token only
through ``OPENAI_API_KEY``, points ``OPENAI_BASE_URL`` at the model gateway,
keeps Qwen Code's home in a per-run temp folder, moves the candidate repo's
Qwen Code settings and agents into a stash folder for the run, and loads a
fixed system settings file that pins the model provider to the gateway.
"""

from __future__ import annotations

import json

from .....exceptions import ServiceError
from .....models.blackbox import BLACKBOX_HARNESS_QWEN_CODE
from ..harness_bridge import parse_plain_output
from . import HarnessSpec, register
from .base import _PROMPT_ARG, ENV_API_KEY, ENV_MODEL, GatewayConfig, HarnessLaunch, _base_env, _npm_install

# npm registry ``dist-tags.latest`` for ``@qwen-code/qwen-code`` on 2026-11-10 (Apache-2.0).
QWEN_CODE_VERSION = "0.27.2"
QWEN_CODE_PACKAGE = "@qwen-code/qwen-code"
# The gateway URL lives in this file rather than on the command line, so a
# protected run's relay rewrite of generated files reaches it.
QWEN_CODE_BASE_URL_FILE = ".app/qwen/base_url"
# Qwen Code 0.27.2 takes the base URL of the ``modelProviders`` entry it picks
# for a model over OPENAI_BASE_URL. The main model is not the only one: the
# ``fastModel``, ``compactionModel``, ``visionModel``, ``imageModel``,
# ``modelFallbacks`` and agent model settings each pick an entry too, and
# ``buildAgentContentGeneratorConfig`` sends that entry's ``envKey`` value (the
# gateway token) to its ``baseUrl``. The run copies this file into its temp
# folder and loads the copy as the system scope. At startup it is the only
# scope with model providers: the run moves the workspace settings aside (see
# :data:`_WORKSPACE_QWEN_FILES`) and the user scope is the empty temp home. So
# every provider entry is at the gateway URL. Qwen Code also watches the user
# and workspace settings files during a run and reloads ``modelProviders``
# when they change, so the system scope pins every key that the reload can
# route (see :data:`_QWEN_CODE_PROTOCOLS`). Its ``model.name`` and
# ``model.baseUrl`` pick the gateway entry for the main model
# (``resolveCliGenerationConfig``). The blank helper models fall back to the
# main model, so no helper model can name another entry. The file holds the
# gateway URL as text, like the base URL file, so a protected run's relay
# rewrite reaches it and both base URLs stay the same.
QWEN_CODE_SETTINGS_FILE = ".app/qwen/settings.json"
# Qwen Code 0.27.2's ``AuthType`` values. ``registerModelProvidersHotReload``
# reloads the merged ``modelProviders`` with the startup ``providerProtocol``
# map, so a reload can register entries only under these keys; the registry
# skips any other key. The system scope merges last and its arrays replace
# the lower scopes' arrays, so the system settings set each key: the gateway
# entry for ``openai`` and no entries for the others.
_QWEN_CODE_PROTOCOLS = (
    "openai",
    "openai-responses",
    "qwen-oauth",
    "qwen-subscription",
    "gemini",
    "vertex-ai",
    "anthropic",
)
# The candidate repo's Qwen Code files that can add a model provider or pick a
# model. Qwen Code 0.27.2 has no flag that skips workspace settings, or turns
# off the settings watcher, and keeps ``--yolo`` and the system settings: an
# untrusted folder forces the ``default`` approval mode, ``--bare`` drops the
# system settings and the AGENTS.md context, and ``tools.executionSandbox``
# is Linux-only and confines the shell. So the run moves these paths into
# :data:`_STASH_DIR` before it starts Qwen Code, and an EXIT trap moves them
# back. A timeout kills the shell with SIGKILL, so the trap does not run; the
# stash then stays, and the next run moves it back before it stashes again.
# The agent can still write ``.qwen/settings.json`` during the run, and the
# trap's restore also writes it. Qwen Code then reloads the provider and
# helper model settings; the system scope pins those, so a reload cannot add
# a provider outside the gateway. When the stash holds an original, the
# restore replaces the agent's file with it. The other ``.qwen`` files
# (``.env``, commands, skills, QWEN.md) stay in place.
_WORKSPACE_QWEN_FILES = ("settings.json", "agents")
# A fixed folder in the workspace, so a stash survives a killed run and each
# move is a rename on one filesystem. Its ``.gitignore`` keeps the folder out
# of the version patch. A version reset (``git clean -fdx``) removes it, but
# the reset also brings back the committed ``.qwen`` files.
_STASH_DIR = ".skynet-qwen-stash"
# The first line of the stash's ``.gitignore``. A run restores and removes
# only a folder with this mark, so a candidate repo's own file or folder at
# :data:`_STASH_DIR` stops the run and stays.
_STASH_MARK = "# Skynet Qwen Code stash"
# The ``model.reasoningEffort`` values that the Qwen Code 0.27.2 settings
# schema accepts. Skynet passes them unchanged and refuses any other level.
QWEN_CODE_EFFORTS = ("none", "low", "medium", "high", "xhigh", "max")


def _qwen_code_settings(base_url: str, reasoning_effort: str | None) -> str:
    """Build the fixed system settings file that pins the model provider to the gateway.

    Args:
        base_url: The gateway base URL.
        reasoning_effort: The user's effort for the model, or ``None`` for Qwen Code's default.

    Returns:
        The settings JSON. It names the key's env variable and holds no key.
    """
    model: dict[str, str] = {"name": f"${{{ENV_MODEL}}}", "baseUrl": base_url}
    if reasoning_effort:
        model["reasoningEffort"] = reasoning_effort
    # ``$version`` 4 is the current settings schema, so Qwen Code does not
    # write a migrated copy back to the file.
    # Empty helper models fall back to the main model, and ``inherit`` gives
    # the Explore agent the main model. They also win over a user scope value.
    providers: dict[str, list[dict[str, str]]] = {protocol: [] for protocol in _QWEN_CODE_PROTOCOLS}
    providers["openai"] = [{"id": f"${{{ENV_MODEL}}}", "baseUrl": base_url, "envKey": "OPENAI_API_KEY"}]
    settings = {
        "$version": 4,
        "modelProviders": providers,
        "providerProtocol": {"openai": "openai"},
        "model": model,
        "fastModel": "",
        "compactionModel": "",
        "visionModel": "",
        "imageModel": "",
        "modelFallbacks": "",
        "agents": {"builtin": {"exploreModel": "inherit"}},
    }
    return json.dumps(settings, indent=2)


# Qwen Code 0.27.2 loads a key from a workspace ``.env`` or ``.qwen/.env`` file
# when the process env holds no value or an empty one, so an empty value would
# let the candidate repo refill it. A non-empty value that no provider accepts
# keeps the key out of reach.
_PROTECTED_KEY_VALUE = "skynet-protected-unset"
# Qwen Code reads these keys for its other auth types and providers. A
# protected run overrides them, so an inherited or workspace key never gives
# Qwen Code a route around the gateway.
_PROTECTED_ENV = {
    "QWEN_API_KEY": _PROTECTED_KEY_VALUE,
    # Also the fallback key of the web search providers.
    "DASHSCOPE_API_KEY": _PROTECTED_KEY_VALUE,
    "ANTHROPIC_API_KEY": _PROTECTED_KEY_VALUE,
    "ANTHROPIC_AUTH_TOKEN": _PROTECTED_KEY_VALUE,
    "GEMINI_API_KEY": _PROTECTED_KEY_VALUE,
    "GOOGLE_API_KEY": _PROTECTED_KEY_VALUE,
    "OPENROUTER_API_KEY": _PROTECTED_KEY_VALUE,
    # Any non-empty QWEN_OAUTH selects the OAuth sign-in in
    # ``getAuthTypeFromEnv``, so this key stays empty. A workspace refill has
    # no effect on the run: ``--auth-type openai`` wins over the env.
    "QWEN_OAUTH": "",
    # A non-empty ENABLE_WEB_SEARCH wins over the workspace
    # ``tools.webSearch.enabled`` setting.
    "ENABLE_WEB_SEARCH": "false",
}


def _qwen_code_launch(model: str, gateway: GatewayConfig, *, reasoning_effort: str | None = None) -> HarnessLaunch:
    """Build the launch for Qwen Code (``qwen``, pinned to :data:`QWEN_CODE_VERSION`).

    Each run gets a fresh ``QWEN_HOME`` outside the workspace, so the settings,
    chat history and installation id never land in the files the run hands
    back. The run first puts back a stash that a killed run left, then moves
    the workspace :data:`_WORKSPACE_QWEN_FILES` into :data:`_STASH_DIR`. It
    stops before Qwen Code starts if :data:`_STASH_DIR` exists and is not a
    Skynet stash. An EXIT trap puts them back, removes the temp folder and
    keeps Qwen Code's exit status. A restore that fails keeps the stash and
    says so on stderr. The plain text that ``--prompt`` mode prints is the
    answer.

    Args:
        model: Target model id.
        gateway: Gateway URL and key.
        reasoning_effort: The user's effort for the model, or ``None`` for
            Qwen Code's default.

    Returns:
        The launch.

    Raises:
        ServiceError: When ``reasoning_effort`` is not a level Qwen Code supports.
    """
    if reasoning_effort and reasoning_effort not in QWEN_CODE_EFFORTS:
        raise ServiceError(
            f"The Qwen Code harness does not support the effort level {reasoning_effort!r}. "
            f"Supported levels: {', '.join(QWEN_CODE_EFFORTS)}."
        )
    # The token goes in OPENAI_API_KEY, never ``--openai-api-key``, so it stays
    # out of the process arguments. The inline OPENAI_BASE_URL replaces any
    # inherited one, so model calls reach only the gateway or its relay. The
    # system settings copy sits in the temp folder, outside the workspace the
    # agent edits, and a workspace ``.env`` cannot set its paths. A workspace
    # ``proxy`` setting or a workspace ``.env`` proxy variable would route the
    # model calls through another host. Qwen Code's undici proxy agent reads
    # ``no_proxy`` from the env, and ``*`` makes every call skip the proxy.
    # Workspace settings can turn on web tools and MCP servers, which call
    # outside the gateway. ``--exclude-tools`` adds to the deny list after the
    # settings, and the tool scheduler denies listed tools also under
    # ``--yolo``. ``--allowed-mcp-server-names`` replaces the settings allow
    # list with a name no server has, and ``--disable-mcp-server '*'`` marks
    # every server disabled, so discovery starts none.
    files = " ".join(_WORKSPACE_QWEN_FILES)
    # The stash goes only after every file is back, so a failed move (``.qwen``
    # is now a file, a full disk) never loses the originals.
    restore = (
        "_qwen_restore() { _ok=0; "
        f"for f in {files}; do "
        f'if [ -e "{_STASH_DIR}/$f" ] || [ -L "{_STASH_DIR}/$f" ]; then '
        f'{{ mkdir -p .qwen && rm -rf ".qwen/$f" && mv "{_STASH_DIR}/$f" ".qwen/$f"; }} || _ok=1; fi; done; '
        f'if [ "$_ok" = 0 ]; then rm -rf {_STASH_DIR}; else '
        f'echo "Skynet could not restore .qwen/settings.json or .qwen/agents. The originals stay in {_STASH_DIR}." >&2; '
        'fi; return "$_ok"; }'
    )
    # One ``&&`` chain: a protected run puts the relay rewrite and ``&&`` in
    # front, so a failed rewrite must stop the whole command, not only the
    # function definition.
    run_command = (
        f"{restore} && "
        # A kill between ``mkdir`` and the mark leaves an empty folder, and
        # git keeps no empty folder, so an empty one is never the repo's.
        f"{{ rmdir {_STASH_DIR} 2>/dev/null || true; }} && "
        f"if [ -e {_STASH_DIR} ] || [ -L {_STASH_DIR} ]; then "
        f'if [ -d {_STASH_DIR} ] && [ ! -L {_STASH_DIR} ] && [ "$(head -n 1 {_STASH_DIR}/.gitignore 2>/dev/null)" = "{_STASH_MARK}" ]; '
        "then _qwen_restore || exit 1; else "
        f'echo "Skynet stopped: {_STASH_DIR} is in the repo and is not a Skynet stash. Remove or rename it." >&2; exit 1; '
        "fi; fi && "
        'export QWEN_HOME="$(mktemp -d)" && '
        "trap '_qwen_restore; rm -rf \"$QWEN_HOME\"' EXIT && "
        f"mkdir {_STASH_DIR} && printf '{_STASH_MARK}\\n*\\n' > {_STASH_DIR}/.gitignore && "
        f"for f in {files}; do "
        f'if [ -e ".qwen/$f" ] || [ -L ".qwen/$f" ]; then mv ".qwen/$f" "{_STASH_DIR}/$f" || exit 1; fi; '
        "done && "
        f'cp {QWEN_CODE_SETTINGS_FILE} "$QWEN_HOME/system-settings.json" && '
        'QWEN_CODE_SYSTEM_SETTINGS_PATH="$QWEN_HOME/system-settings.json" '
        "NO_PROXY='*' no_proxy='*' "
        f'OPENAI_BASE_URL="$(cat {QWEN_CODE_BASE_URL_FILE})" '
        f'OPENAI_API_KEY="${ENV_API_KEY}" OPENAI_MODEL="${ENV_MODEL}" '
        "qwen --yolo --auth-type openai "
        "--exclude-tools web_fetch,web_search "
        "--allowed-mcp-server-names skynet-no-mcp --disable-mcp-server '*' "
        f"--prompt {_PROMPT_ARG} < /dev/null"
    )
    env = _base_env(model, gateway)
    env.update(
        {
            "QWEN_TELEMETRY_ENABLED": "false",
            "QWEN_USAGE_STATISTICS_ENABLED": "false",
            # Skips the startup npm update check and the auto-install it can start.
            "QWEN_CODE_SKIP_UPDATE_CHECK_ONCE": "true",
            # Stops the models.dev catalog download, a network call outside the gateway.
            "QWEN_CODE_MODELS_DEV": "off",
        }
    )
    return HarnessLaunch(
        instructions_file="AGENTS.md",
        install_command=_npm_install("qwen", QWEN_CODE_PACKAGE, QWEN_CODE_VERSION),
        files={
            QWEN_CODE_BASE_URL_FILE: gateway.url,
            QWEN_CODE_SETTINGS_FILE: _qwen_code_settings(gateway.url, reasoning_effort),
        },
        run_command=run_command,
        env=env,
        parse_output=parse_plain_output,
    )


SPEC = register(
    HarnessSpec(
        id=BLACKBOX_HARNESS_QWEN_CODE,
        launch=_qwen_code_launch,
        binary="qwen",
        version=QWEN_CODE_VERSION,
        output_format="plain",
        protected_env=_PROTECTED_ENV,
        position=58,
        supported_efforts=QWEN_CODE_EFFORTS,
    )
)
