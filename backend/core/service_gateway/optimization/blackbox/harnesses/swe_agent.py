"""SWE-agent harness (``swe_agent``).

Adapted from Harbor 0.25.0 (Apache-2.0), agents/installed/swe_agent.py: the
install from a shallow clone of one SWE-agent tag into a uv venv, the
``sweagent run`` one-shot on the built-in default config with the workspace as
a ``preexisting`` repo on a ``local`` deployment, the LiteLLM ``openai/``
route, the zero cost limits, ``top_p=null``, and ``temperature=1`` with an
active reasoning effort. Harbor floats uv and the dependencies, gives SWE-agent
a direct provider key and keeps the trajectory in the agent logs; Skynet pins
uv, the tag and its commit, LiteLLM, SWE-ReX and a resolution date, sends the
effort in ``extra_body`` with temperature 1 for every level, points
LiteLLM at the model gateway through a launch file, reads the key from the
environment, and keeps the task and the trajectory in a per-run folder it
removes. SWE-agent's transcript goes to stderr; stdout carries only the patch
that SWE-agent submits.
"""

from __future__ import annotations

import json
import shlex

from .....exceptions import ServiceError
from .....models.blackbox import BLACKBOX_HARNESS_SWE_AGENT
from ..harness_bridge import parse_plain_output
from . import HarnessSpec, register
from .base import ENV_API_KEY, ENV_MODEL, ENV_PROMPT_FILE, GatewayConfig, HarnessLaunch, _base_env, _version_check

# Git tag v1.1.0 of SWE-agent/SWE-agent (MIT), 2025-05-22. The PyPI name
# ``sweagent`` is an unrelated 0.0.1 upload, so the install uses the tag.
SWE_AGENT_VERSION = "1.1.0"
SWE_AGENT_COMMIT = "0f3acafacabc0def8cc76b4e48acb4b6cf302cb9"
SWE_AGENT_REPO_URL = "https://github.com/SWE-agent/SWE-agent.git"
# The same uv and Python pins as the Aider and mini-swe-agent harnesses, so one
# image layer serves all of them.
SWE_AGENT_UV_VERSION = "0.13.7"
SWE_AGENT_PYTHON = "3.12"
# SWE-agent does not pin its dependencies. The backend locks and runs this
# LiteLLM; SWE-ReX is the release current at the tag's resolution date.
SWE_AGENT_LITELLM_VERSION = "1.103.1"
SWE_AGENT_SWEREX_VERSION = "1.4.0"
# The levels LiteLLM 1.103.1 accepts as ``reasoning_effort``
# (``types.llms.openai.REASONING_EFFORT``). Each passes unchanged; submit and
# the launch refuse any other level (SKY-585).
SWE_AGENT_EFFORTS = ("none", "minimal", "low", "medium", "high", "xhigh", "max")
# uv resolves every other dependency as of this date, so a later release of
# one of them never reaches a run. It is after the LiteLLM pin's release.
SWE_AGENT_EXCLUDE_NEWER = "2026-10-01T00:00:00Z"
# The gateway URL lives in this file rather than on the command line, so a
# protected run's relay rewrite of launch files reaches it.
SWE_AGENT_BASE_URL_FILE = ".app/swe_agent/base_url"
# SWE-agent 1.1.0 installs its tools under /root/tools and writes /root/state.json
# and /root/model.patch, all hard-coded. Without a writable /root it fails deep
# in its tool setup, so the run stops first with a clear message.
SWE_AGENT_ROOT_GUARD = (
    "[ -w /root ] || { echo 'swe_agent: SWE-agent 1.1.0 needs a writable /root for its tools' >&2; exit 1; }"
)
_HOME = '"$HOME/.local/share/swe-agent"'
_VENV_PYTHON = '"$HOME/.local/share/swe-agent/venv/bin/python"'
# SWE-agent sends an ``openai/`` model to the ``api_base`` through LiteLLM; the
# gateway receives the id after the prefix.
_LITELLM_PREFIX = "openai/"
# SWE-agent has no ``--version`` option, so the probe asks the venv's Python
# for the installed distributions. It reads package metadata only: importing
# ``sweagent`` asserts that its config folders exist. A venv with another
# LiteLLM or SWE-ReX prints those versions instead, so the pinned check fails.
# ``-I`` keeps the workspace off ``sys.path``. ``--version`` lands in
# ``sys.argv`` and is ignored.
_PROBE = f"{_VENV_PYTHON} -I -c " + shlex.quote(
    'import importlib.metadata as m; d = (m.version("litellm"), m.version("swe-rex")); '
    f'print(m.version("sweagent") if d == ("{SWE_AGENT_LITELLM_VERSION}", "{SWE_AGENT_SWEREX_VERSION}") '
    'else "litellm %s swe-rex %s" % d)'
)
# The preexisting-repo reset runs ``git clean -fdq``, which deletes untracked
# files, so every run starts from a snapshot commit of the workspace. A resumed
# run's workspace is already a repo with the earlier run's edits; committing
# them keeps the submitted patch to this run's edits only. ``git add -A`` is
# bounded as in the Aider harness, so a huge workspace cannot stall the start.
# A workspace inside an outer repo gets its own repo: otherwise the snapshot
# commits the outer repo's files and the patch paths carry a folder prefix.
# ``pwd -P`` matches the resolved path that ``--show-toplevel`` prints.
# ``--no-verify`` skips only two hooks, so the commit also turns off the hooks
# folder and signing: a failing hook or a missing gpg would stop the snapshot.
_SNAPSHOT_WORKSPACE = (
    "_swe_bound=; command -v timeout >/dev/null 2>&1 && _swe_bound='timeout 120'; "
    '{ [ "$(git rev-parse --show-toplevel 2>/dev/null)" = "$(pwd -P)" ] || git init -q; } && '
    "$_swe_bound git add -A && git -c user.name=Agent -c user.email=agent@localhost "
    "-c core.hooksPath=/dev/null -c commit.gpgsign=false "
    "commit -q --no-verify --allow-empty -m 'Task files before agent run'"
)
# Telemetry stays off in every run. LiteLLM loads a ``.env`` from the workspace
# in its default DEV mode.
_TELEMETRY_ENV = {
    "HF_HUB_DISABLE_TELEMETRY": "1",
    "DO_NOT_TRACK": "1",
    "LITELLM_MODE": "PRODUCTION",
}
# SWE-agent and LiteLLM read these keys for their other routes. A protected run
# blanks them, so an inherited key never gives SWE-agent a route around the
# gateway. The gateway key reaches SWE-agent as a ``$APP_API_KEY`` reference.
_PROTECTED_ENV = {
    "OPENAI_API_KEY": "",
    "ANTHROPIC_API_KEY": "",
    "GOOGLE_API_KEY": "",
    "GEMINI_API_KEY": "",
    "OPENROUTER_API_KEY": "",
    "GROQ_API_KEY": "",
    "XAI_API_KEY": "",
    "MISTRAL_API_KEY": "",
    "DEEPSEEK_API_KEY": "",
    "AZURE_API_KEY": "",
    "AZURE_OPENAI_API_KEY": "",
    "OPENAI_BASE_URL": "",
    "OPENAI_API_BASE": "",
    # A protected sandbox has no route to the internet: LiteLLM keeps its
    # bundled cost map instead of fetching one.
    "LITELLM_LOCAL_MODEL_COST_MAP": "True",
}


def _swe_agent_install() -> str:
    """Return the command that pins SWE-agent to :data:`SWE_AGENT_VERSION` and logs the version that will run.

    Returns:
        The shell command. It clones the tag at depth 1, checks the commit,
        and installs the clone editable, with pinned LiteLLM and SWE-ReX, into
        a uv venv. SWE-agent finds its config and tool bundles relative to its
        source folder, so a plain install from the clone cannot start.
        It removes the partial install folder and exits non-zero when a step
        fails.
    """
    check = _version_check(_PROBE, SWE_AGENT_VERSION)
    # The bootstrap runs this inside its own ``set -eu`` script, so the step
    # records its status with ``|| r=$?`` (exempt from ``set -e``) and removes
    # the folder before it fails, rather than with an EXIT trap that would
    # replace the caller's.
    return (
        f'export PATH="$HOME/.local/bin:$PATH"; h={_HOME}; '
        f"{check} || {{ r=0; "
        "{ command -v uv >/dev/null 2>&1 || "
        f"curl -LsSf https://astral.sh/uv/{SWE_AGENT_UV_VERSION}/install.sh | sh; }} && "
        'rm -rf "$h" && mkdir -p "$h" && '
        f'git -c advice.detachedHead=false clone -q --depth 1 --branch v{SWE_AGENT_VERSION} {SWE_AGENT_REPO_URL} "$h/src" && '
        f'[ "$(git -C "$h/src" rev-parse HEAD)" = {SWE_AGENT_COMMIT} ] && '
        f'uv venv -q --python {SWE_AGENT_PYTHON} "$h/venv" && '
        f'uv pip install -q --python "$h/venv/bin/python" --exclude-newer {SWE_AGENT_EXCLUDE_NEWER} '
        f'-e "$h/src" swe-rex=={SWE_AGENT_SWEREX_VERSION} litellm=={SWE_AGENT_LITELLM_VERSION} >/dev/null || r=$?; '
        '[ "$r" -eq 0 ] || { rm -rf "$h"; exit "$r"; }; } '
        f"&& {_PROBE}"
    )


def _effort_flags(reasoning_effort: str | None) -> str:
    """Return the ``sweagent run`` flags for the user's effort.

    The level goes into ``completion_kwargs.extra_body``, so LiteLLM 1.103.1
    puts it in the request body as is. As a ``reasoning_effort`` argument,
    LiteLLM checks it against the model id: any id with ``gpt-5`` goes through
    ``OpenAIGPT5Config``, which refuses ``xhigh`` for ``gpt-5``, ``gpt-5-mini``
    and ``gpt-5.1`` and ``minimal`` for ``gpt-5.1``, ``gpt-5.2`` and
    ``gpt-5.5``. ``gpt-4o`` refuses every level. For ``gpt-5.5`` with tools,
    LiteLLM sends every level except ``none`` to ``/v1/responses``, which the
    gateway may not serve. The ``extra_body`` path avoids all of these
    problems. The gateway, not LiteLLM, decides on the level. LiteLLM
    also refuses SWE-agent's default temperature 0 for OpenAI reasoning models
    when it does not see the level, so every level, ``none`` too, sets
    temperature 1.

    Args:
        reasoning_effort: The user's effort for the model, or ``None`` for the model's default.

    Returns:
        The flags with a leading space, or an empty string.

    Raises:
        ServiceError: When the level is not in :data:`SWE_AGENT_EFFORTS`.
    """
    if not reasoning_effort:
        return ""
    if reasoning_effort not in SWE_AGENT_EFFORTS:
        raise ServiceError(
            f"The SWE-agent harness does not support the effort level '{reasoning_effort}'. "
            f"Supported levels: {', '.join(SWE_AGENT_EFFORTS)}."
        )
    kwargs = shlex.quote(json.dumps({"extra_body": {"reasoning_effort": reasoning_effort}}))
    return f" --agent.model.completion_kwargs={kwargs} --agent.model.temperature=1"


def _swe_agent_launch(model: str, gateway: GatewayConfig, *, reasoning_effort: str | None = None) -> HarnessLaunch:
    """Build the launch for SWE-agent (``sweagent``, pinned to :data:`SWE_AGENT_VERSION`).

    Each run gets a fresh folder outside the workspace for the task file, an
    empty ``.env`` and SWE-agent's output. An EXIT trap removes it. AGENTS.md
    and the prompt reach the task file through ``cat``, never through shell
    words. The key stays in the environment: SWE-agent reads ``$APP_API_KEY``
    itself. The command keeps SWE-agent's exit status and prints the submitted
    patch.

    Args:
        model: Target model id.
        gateway: Gateway URL and key.
        reasoning_effort: The user's effort for the model, or ``None`` for the model's default.

    Returns:
        The launch.

    Raises:
        ServiceError: When the effort level is not in :data:`SWE_AGENT_EFFORTS`.
    """
    run_command = (
        f"{SWE_AGENT_ROOT_GUARD}; "
        f'h={_HOME} && d="$(mktemp -d)" && '
        "trap 'rm -rf \"$d\"' EXIT && "
        f"{{ {_SNAPSHOT_WORKSPACE}; }} && "
        # SWE-agent reads these fixed paths again when a submission fails, so a
        # copy from an earlier run in a reused sandbox must not stand in.
        "{ rm -f /root/model.patch /root/state.json 2>/dev/null || :; } && "
        f'{{ if [ -f AGENTS.md ]; then cat AGENTS.md; echo; fi; cat "${ENV_PROMPT_FILE}"; }} > "$d/problem.md" && '
        ': > "$d/empty.env" && '
        "{ status=0; "
        '"$h/venv/bin/sweagent" run '
        f'--agent.model.name="{_LITELLM_PREFIX}${ENV_MODEL}" '
        f'--agent.model.api_base="$(cat {SWE_AGENT_BASE_URL_FILE})" '
        # SWE-agent reads a ``$NAME`` api_key from the environment, so the key
        # never lands on argv. LiteLLM has no prices for gateway model ids, and
        # a cost limit then stops the run; Skynet's run budget bounds the spend.
        f"--agent.model.api_key='${ENV_API_KEY}' "
        "--agent.model.per_instance_cost_limit=0 --agent.model.total_cost_limit=0 "
        "--agent.model.max_input_tokens=0 --agent.model.top_p=null"
        f"{_effort_flags(reasoning_effort)} "
        '--problem_statement.path="$d/problem.md" '
        '--env.deployment.type=local --env.repo.type=preexisting --env.repo.repo_name="$PWD" '
        # Without an env file, SWE-agent loads ``.env`` from the workspace.
        '--output_dir="$d/out" --env_var_path="$d/empty.env" < /dev/null >&2 || status=$?; '
        'cat "$d"/out/*/*.patch 2>/dev/null; (exit "$status"); }'
    )
    env = _base_env(model, gateway)
    env.update(_TELEMETRY_ENV)
    return HarnessLaunch(
        instructions_file="AGENTS.md",
        install_command=_swe_agent_install(),
        files={SWE_AGENT_BASE_URL_FILE: gateway.url},
        run_command=run_command,
        env=env,
        parse_output=parse_plain_output,
    )


SPEC = register(
    HarnessSpec(
        id=BLACKBOX_HARNESS_SWE_AGENT,
        launch=_swe_agent_launch,
        binary=_PROBE,
        version=SWE_AGENT_VERSION,
        output_format="plain",
        protected_env=_PROTECTED_ENV,
        position=54,
        supported_efforts=SWE_AGENT_EFFORTS,
    )
)
