"""OpenHands harness (``openhands``).

Adapted from Harbor 0.25.0 (Apache-2.0), agents/installed/openhands: the uv
virtualenv install of ``openhands-ai`` with ``SKIP_VSCODE_BUILD``, the local
runtime settings (``RUNTIME=local``, ``SKIP_DEPENDENCY_CHECK``, no browser)
and the headless ``python -m openhands.core.main`` one-shot with
``SANDBOX_VOLUMES`` on the working directory and logs on stdout. Harbor passes
the task inline with ``--task`` and turns prompt extensions off. Skynet passes
a task file with ``-f`` and points LiteLLM at the model gateway.

Headless OpenHands 1.6.0 never reads the workspace ``AGENTS.md``: it loads
repo instructions only from ``.openhands_instructions`` and
``.openhands/microagents``, and it resolves the ``AGENTS.md`` next to those
microagents in a temporary copy. So the run puts the candidate's
``AGENTS.md`` in front of the prompt in the task file that ``-f`` names.
"""

from __future__ import annotations

from .....models.blackbox import BLACKBOX_HARNESS_OPENHANDS
from ..harness_bridge import parse_plain_output
from . import HarnessSpec, register
from .base import ENV_API_KEY, ENV_MODEL, ENV_PROMPT_FILE, GatewayConfig, HarnessLaunch, _base_env, _version_check

# PyPI ``openhands-ai`` 1.6.0 (MIT), 2026-03-30. It is the last release with
# the headless ``openhands.core.main`` entry point; 1.7.0 removed it.
OPENHANDS_VERSION = "1.6.0"
OPENHANDS_PACKAGE = "openhands-ai"
# ``openhands-ai`` 1.6.0 asks only for ``litellm>=1.74.3``, and its ``openai==2.8``
# pin caps LiteLLM at 1.80.10 today. The pin keeps the effort and sampling
# behavior that the tests and the PR checked from moving with new releases.
OPENHANDS_LITELLM_VERSION = "1.80.10"
OPENHANDS_PYTHON = "3.12"
OPENHANDS_UV_VERSION = "0.13.7"
OPENHANDS_VENV = '"$HOME/.local/share/openhands-venv"'
# The gateway URL lives in this file rather than on the command line, so a
# protected run's relay rewrite of generated files reaches it.
OPENHANDS_BASE_URL_FILE = ".app/openhands/base_url"
# OpenHands runs on LiteLLM, which routes an ``openai/`` model to the
# OpenAI-style base URL; the gateway receives the id after the prefix.
_LITELLM_PREFIX = "openai/"
_INSTRUCTIONS_FILE = "AGENTS.md"
# Prints the ``agent_state`` of the last ``agent_state_changed`` event in the
# session's event files (``<id>.json``, one ``json.dumps`` line each).
# ``openhands.core.main`` exits 0 when the agent ends in ``error``, so the run
# reads the final state here instead.
_FINAL_STATE_AWK = (
    "BEGIN { last = -1 } "
    'FNR == 1 { id = FILENAME; sub(/.*\\//, "", id); sub(/\\.json$/, "", id); id += 0 } '
    '/"observation": "agent_state_changed"/ && id > last && match($0, /"agent_state": "[a-z_]+"/) '
    "{ state = substr($0, RSTART + 16, RLENGTH - 17); last = id } "
    "END { print state }"
)
_LOCAL_BIN = '"$HOME/.local/bin"'
# The wheel ships no console script, so the install writes this wrapper; the
# shared pin check then runs ``openhands --version`` like any other harness.
# ``-P`` keeps the workspace off ``sys.path``: ``python -m`` puts the working
# directory first, so a candidate's ``json.py`` would shadow the stdlib.
_WRAPPER = '#!/bin/sh\\nexec "%s/bin/python" -P -m openhands.core.main "$@"\\n'
# OpenHands 1.6.0 sends ``LLM_REASONING_EFFORT`` only for model names in its
# own reasoning list, which misses models such as ``claude-opus-5.5`` and
# ``gpt-6``. Its ``completion_kwargs`` reach every LiteLLM call, so the
# effort goes there. For a model outside that list, OpenHands also sends its
# defaults ``temperature=0.0`` and ``top_p=1.0``: Anthropic Opus 4.1 and later
# reject the two together, and OpenAI reasoning models reject
# ``temperature=0``. ``None`` overrides them, and LiteLLM drops a ``None``
# parameter, so the gateway gets neither. LiteLLM drops ``reasoning_effort``
# for a model name it does not know, so ``allowed_openai_params`` forces it
# through. OpenHands also adds ``thinking`` for a model whose name is
# ``gemini-2.5-pro`` (a 128-token budget, with ``allowed_openai_params``
# set to ``["thinking"]``) or contains ``claude-opus-4-1`` (disabled),
# because the harness never sets ``LLM_REASONING_EFFORT``. With
# ``LLM_DROP_PARAMS=false``, LiteLLM 1.80.10 refuses that ``thinking`` on
# the ``openai/`` route, so every such run fails. ``None`` removes it. ``allowed_openai_params=None``
# also removes the OpenHands ``["thinking"]``: with no effort, LiteLLM would
# otherwise pass ``thinking=None`` to the OpenAI client, which rejects it.
_DEFAULTS_OFF = {"temperature": None, "top_p": None, "thinking": None, "allowed_openai_params": None}
# LiteLLM reads these keys for its other providers. A protected run blanks
# them, so an inherited key never gives OpenHands a route around the gateway.
_PROTECTED_ENV = {
    "OPENAI_API_KEY": "",
    "ANTHROPIC_API_KEY": "",
    "GOOGLE_API_KEY": "",
    "GEMINI_API_KEY": "",
    "OPENROUTER_API_KEY": "",
    "AZURE_API_KEY": "",
    "AZURE_OPENAI_API_KEY": "",
    "GROQ_API_KEY": "",
    "XAI_API_KEY": "",
    "MISTRAL_API_KEY": "",
    "DEEPSEEK_API_KEY": "",
    # The Tavily search tool calls an outside service with this key.
    "SEARCH_API_KEY": "",
    # A protected sandbox has no route to the internet: LiteLLM keeps its
    # bundled cost map instead of fetching one.
    "LITELLM_LOCAL_MODEL_COST_MAP": "True",
    "DO_NOT_TRACK": "1",
}


def _openhands_install() -> str:
    """Return a command that installs the pinned OpenHands release unless it is already on PATH.

    Harbor installs uv and Python, then the package, into ``/opt``; this pins
    uv and the package and keeps everything under ``$HOME``.

    Returns:
        The shell command. It creates a uv virtualenv, installs
        ``openhands-ai`` into it, writes the ``openhands`` wrapper into
        ``$HOME/.local/bin`` and logs the version that will run. It exits
        non-zero when a step fails.
    """
    # The bootstrap runs this inside its own ``set -eu`` script, where a
    # failure inside an ``||`` list does not stop the script, so the step
    # exits with the failed status itself.
    return (
        f'export PATH={_LOCAL_BIN}:"$PATH"; '
        f"{_version_check('openhands', OPENHANDS_VERSION)} || "
        "{ { command -v uv >/dev/null 2>&1 || "
        f"curl -LsSf https://astral.sh/uv/{OPENHANDS_UV_VERSION}/install.sh | sh; }} && "
        f"uv venv --clear --python {OPENHANDS_PYTHON} {OPENHANDS_VENV} >/dev/null && "
        f"SKIP_VSCODE_BUILD=true uv pip install --python {OPENHANDS_VENV}/bin/python "
        f"{OPENHANDS_PACKAGE}=={OPENHANDS_VERSION} litellm=={OPENHANDS_LITELLM_VERSION} >/dev/null && "
        f"mkdir -p {_LOCAL_BIN} && "
        f"printf '{_WRAPPER}' {OPENHANDS_VENV} > {_LOCAL_BIN}/openhands && "
        f'chmod 755 {_LOCAL_BIN}/openhands || exit "$?"; }}; '
        "openhands --version"
    )


def _openhands_completion_kwargs(reasoning_effort: str | None) -> dict[str, str]:
    """Return the ``LLM_COMPLETION_KWARGS`` setting for every run.

    OpenHands parses the value with ``ast.literal_eval``. It always unsets
    ``temperature``, ``top_p``, ``thinking`` and ``allowed_openai_params``.
    It adds the user's effort unchanged, as the Codex, OpenCode and Aider
    harnesses do, so the gateway gets the same ``reasoning_effort`` from
    every harness. ``LLM_DROP_PARAMS=false`` makes a level that
    LiteLLM refuses fail the call rather than vanish.

    Args:
        reasoning_effort: The user's effort for the model, or ``None`` for the model default.

    Returns:
        The env entry.
    """
    kwargs: dict[str, object] = dict(_DEFAULTS_OFF)
    if reasoning_effort:
        kwargs["reasoning_effort"] = reasoning_effort
        kwargs["allowed_openai_params"] = ["reasoning_effort"]
    return {"LLM_COMPLETION_KWARGS": repr(kwargs)}


def _openhands_launch(model: str, gateway: GatewayConfig, *, reasoning_effort: str | None = None) -> HarnessLaunch:
    """Build the launch for OpenHands (``openhands``, pinned to :data:`OPENHANDS_VERSION`).

    Each run gets a fresh OpenHands root outside the workspace for the file
    store, the cache, the task file, ``TMPDIR`` and ``HOME``. The config path points into
    that root but does not exist, so no user config loads. So
    session files never land in the files the run hands back, a
    ``config.toml`` in the workspace cannot change the model route, and
    OpenHands loads no user microagents, ``AGENTS.md`` or ``config.toml``
    from the real ``HOME``. The agent's shell also sees that ``HOME``. An
    EXIT trap removes the root when the run's shell exits and keeps the exit
    status.

    The task file holds the workspace ``AGENTS.md``, when there is one, then
    the prompt. OpenHands writes its log to stderr, so the run merges it into
    stdout for the bridge, as Harbor does. ``LOG_ALL_EVENTS`` puts the
    agent's actions, with the final finish message, into that log.
    A failed OpenHands exit status stays. After a clean exit, the run exits 1
    unless the agent's final state is ``finished``: an error, a stop, or the
    500-iteration default limit fails the run instead of scoring the log as
    an answer. The run timeout and the run budget bound a run, as for the
    other harnesses, so the run keeps the OpenHands iteration default.

    Args:
        model: Target model id.
        gateway: Gateway URL and key.
        reasoning_effort: The user's effort for the model, or ``None`` for the model default.

    Returns:
        The launch.
    """
    run_command = (
        f'export PATH={_LOCAL_BIN}:"$PATH" && '
        'export OPENHANDS_ROOT="$(mktemp -d)" && '
        "trap 'rm -rf \"$OPENHANDS_ROOT\"' EXIT && "
        'mkdir "$OPENHANDS_ROOT/home" "$OPENHANDS_ROOT/tmp" && '
        f"{{ if [ -f {_INSTRUCTIONS_FILE} ]; then cat {_INSTRUCTIONS_FILE}; printf '\\n\\n'; fi; "
        f'cat "${ENV_PROMPT_FILE}"; }} > "$OPENHANDS_ROOT/task.md" && '
        f'LLM_BASE_URL="$(cat {OPENHANDS_BASE_URL_FILE})" LLM_API_KEY="${ENV_API_KEY}" '
        f'LLM_MODEL="{_LITELLM_PREFIX}${ENV_MODEL}" '
        'FILE_STORE_PATH="$OPENHANDS_ROOT/store" CACHE_DIR="$OPENHANDS_ROOT/cache" '
        'SANDBOX_VOLUMES="$PWD:/workspace:rw" USER="$(id -un)" HOME="$OPENHANDS_ROOT/home" TMPDIR="$OPENHANDS_ROOT/tmp" '
        'openhands --config-file "$OPENHANDS_ROOT/config.toml" '
        '-f "$OPENHANDS_ROOT/task.md" 2>&1 < /dev/null; '
        'status=$?; [ "$status" -eq 0 ] || exit "$status"; '
        f'state="$(awk \'{_FINAL_STATE_AWK}\' "$OPENHANDS_ROOT"/store/sessions/*/events/*.json 2>/dev/null)"; '
        '[ "$state" = finished ] || { echo "OpenHands ended in agent state ${state:-unknown}, not finished." >&2; exit 1; }'
    )
    env = _base_env(model, gateway)
    env.update(
        {
            "RUNTIME": "local",
            # Harbor skips the local runtime's startup check for tmux and
            # libtmux the same way; the sandbox image still needs tmux.
            "SKIP_DEPENDENCY_CHECK": "1",
            "RUN_AS_OPENHANDS": "false",
            "SU_TO_USER": "false",
            "FILE_STORE": "local",
            "LOG_TO_FILE": "false",
            "AGENT_ENABLE_BROWSING": "false",
            "ENABLE_BROWSER": "false",
            "SANDBOX_ENABLE_AUTO_LINT": "true",
            # Harbor turns prompt extensions off. Skynet keeps the OpenHands
            # default, so a candidate's ``.openhands/microagents`` and
            # ``.openhands_instructions`` load. They do not load ``AGENTS.md``.
            "AGENT_ENABLE_PROMPT_EXTENSIONS": "true",
            # The default MCP setup adds OpenHands' own MCP server and tools,
            # which the harness does not configure or route.
            "AGENT_ENABLE_MCP": "false",
            # OpenHands 1.6.0 turns native tool calling on only for model
            # names in its own list, which misses models such as
            # ``claude-opus-5.5`` and ``gpt-6``. The fallback prompts the
            # model to write tool calls as text.
            "LLM_NATIVE_TOOL_CALLING": "true",
            # Without it, OpenHands logs actions at debug level, so the log
            # the bridge returns has no agent message or final answer.
            "LOG_ALL_EVENTS": "true",
            # OpenHands defaults to ``drop_params=True``, so LiteLLM would
            # silently drop an effort that it knows a model does not take,
            # such as ``xhigh`` for ``gpt-5.1``. The call fails instead, and
            # the run ends in a state that is not ``finished``.
            "LLM_DROP_PARAMS": "false",
            **_openhands_completion_kwargs(reasoning_effort),
        }
    )
    return HarnessLaunch(
        instructions_file=_INSTRUCTIONS_FILE,
        install_command=_openhands_install(),
        files={OPENHANDS_BASE_URL_FILE: gateway.url},
        run_command=run_command,
        env=env,
        parse_output=parse_plain_output,
    )


SPEC = register(
    HarnessSpec(
        id=BLACKBOX_HARNESS_OPENHANDS,
        launch=_openhands_launch,
        binary="openhands",
        version=OPENHANDS_VERSION,
        output_format="plain",
        protected_env=_PROTECTED_ENV,
        position=54,
    )
)
