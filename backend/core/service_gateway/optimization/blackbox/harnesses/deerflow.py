"""DeerFlow harness: a pinned DeerFlow venv that runs one agent turn through the gateway.

Adapted from Harbor 0.25.0 (Apache-2.0), agents/installed/deerflow.
"""

from __future__ import annotations

import json
import shlex
from urllib.parse import urlsplit

from .....exceptions import ServiceError
from .....models.blackbox import BLACKBOX_HARNESS_DEERFLOW
from ..harness_bridge import parse_pi_output
from . import HarnessSpec, register
from .base import ENV_API_KEY, ENV_PROMPT_FILE, GatewayConfig, HarnessLaunch, _base_env, _version_check

HARNESS_ID = BLACKBOX_HARNESS_DEERFLOW
DEERFLOW_REPO = "https://github.com/bytedance/deer-flow.git"
# DeerFlow publishes no release of its harness package, so the pin is the exact
# source commit Harbor 0.25.0 tests against. The install records it in the venv.
DEERFLOW_REF = "7a6c4a994a86583d2a3c056ee9d0f157d4f030c2"
DEERFLOW_PACKAGE = "deerflow-harness"
# The same uv release the native engines install, so one pin covers the box.
_UV_PIN = "uv==0.9.13"
_PYTHON = "3.12"
_UV_PATH = 'PATH="$HOME/.local/bin:$PATH"'
_HOME_DIR = "$HOME/.app/deerflow"
_REPO_DIR = f"{_HOME_DIR}/deer-flow"
_VENV_DIR = f"{_HOME_DIR}/venv"
_VENV_PYTHON = f"{_VENV_DIR}/bin/python"
_REF_MARKER = "deerflow-ref"
STATE_DIR = ".app/deerflow"
CONFIG_FILE = f"{STATE_DIR}/config.json"
RUNNER_FILE = f"{STATE_DIR}/run_agent.py"
_RUNNER = f'"{_VENV_PYTHON}" {RUNNER_FILE}'
# DeerFlow writes config.yaml, threads and checkpoints under these roots. They
# live outside the workspace, so none of it enters a version patch.
_DATA_DIR = "DEERFLOW_DATA_DIR"
_RUN_COMMAND = (
    f'export {_DATA_DIR}="$(mktemp -d)" && '
    f'DEER_FLOW_PROJECT_ROOT="${_DATA_DIR}/project" DEER_FLOW_HOME="${_DATA_DIR}/home" {_RUNNER}'
)
# The probe needs no workspace file, so the protected bootstrap can run it
# before any launch file exists. ``python -c`` ignores the ``--version`` arg.
_VERSION_PROBE = (
    "import importlib.metadata as m, pathlib, sys; "
    f"m.version({DEERFLOW_PACKAGE!r}); "
    f"print((pathlib.Path(sys.prefix) / {_REF_MARKER!r}).read_text().strip())"
)
_VERSION_BINARY = f'"{_VENV_PYTHON}" -c {shlex.quote(_VERSION_PROBE)}'
MODEL_NAME = "skynet-model"
RECURSION_LIMIT = 1000
ENV_THINKING = "DEERFLOW_THINKING_ENABLED"
# Empty values override any key the host shell leaks into the box. DeerFlow
# then has no direct provider route and no search, tracing or telemetry key.
_NO_DIRECT_KEYS = dict.fromkeys(
    (
        "OPENAI_API_KEY",
        "ANTHROPIC_API_KEY",
        "GEMINI_API_KEY",
        "GOOGLE_API_KEY",
        "DEEPSEEK_API_KEY",
        "OPENROUTER_API_KEY",
        "TAVILY_API_KEY",
        "SERPER_API_KEY",
        "EXA_API_KEY",
        "FIRECRAWL_API_KEY",
        "JINA_API_KEY",
        "LANGSMITH_API_KEY",
        "LANGFUSE_PUBLIC_KEY",
        "LANGFUSE_SECRET_KEY",
    ),
    "",
)
_NO_TRACING = {"LANGSMITH_TRACING": "false", "LANGCHAIN_TRACING_V2": "false", "DO_NOT_TRACK": "1"}
# A protected sandbox has no route to the internet: uv must use its cache and
# nothing may phone home or update itself.
_PROTECTED_ENV = {"UV_OFFLINE": "1", "UV_NO_PROGRESS": "1", **_NO_TRACING, **_NO_DIRECT_KEYS}
_URL_FORBIDDEN = frozenset("\"'\\")
# Harbor's default tools without the web group: web search and fetch need the
# internet and a search key, and neither is allowed in the box.
_TOOLS = (
    ("ls", "file:read", "ls_tool"),
    ("read_file", "file:read", "read_file_tool"),
    ("glob", "file:read", "glob_tool"),
    ("grep", "file:read", "grep_tool"),
    ("write_file", "file:write", "write_file_tool"),
    ("str_replace", "file:write", "str_replace_tool"),
    ("bash", "bash", "bash_tool"),
)

_RUNNER_SCRIPT = """\"\"\"Run one DeerFlow agent turn through the Skynet model gateway.

Adapted from Harbor 0.25.0 (Apache-2.0), agents/installed/deerflow_runner.
\"\"\"

import importlib
import json
import os
import sys
from pathlib import Path

CONFIG_FILE = Path({config_file!r})
GATEWAY_URL = {gateway_url!r}
DIRECT_ROUTES = {direct_routes!r}
STEERING = (
    "You are operating directly on a real Linux machine through the bash and file tools. "
    "Act on the actual filesystem at real absolute paths.\\n"
    "Your working directory is {{workdir}}. Create and modify the task's files under {{workdir}}. "
    "Do NOT write deliverables into /mnt/user-data.\\n\\nTask:\\n{{task}}"
)


def _flag(name, default):
    \"\"\"Read a boolean toggle from the environment.\"\"\"
    value = os.environ.get(name)
    return default if value is None else value.strip().lower() in ("1", "true", "yes", "on")


def _count(usage, field):
    \"\"\"Return one usage counter, or 0 when DeerFlow did not report a valid one.\"\"\"
    value = usage.get(field) if isinstance(usage, dict) else None
    return value if isinstance(value, int) and not isinstance(value, bool) and value >= 0 else 0


def _text(content):
    \"\"\"Return the plain text of a LangChain message content value.\"\"\"
    if isinstance(content, str):
        return content
    if isinstance(content, list):
        return "".join(part.get("text", "") for part in content if isinstance(part, dict))
    return ""


def _write_config(workdir):
    \"\"\"Write DeerFlow's config.yaml with the gateway model and the workdir mount.

    JSON is valid YAML, so the config needs no YAML writer here.
    \"\"\"
    config = json.loads(CONFIG_FILE.read_text(encoding="utf-8"))
    # Any other model route would bypass the gateway's metering and keys.
    if [model.get("base_url") for model in config["models"]] != [GATEWAY_URL]:
        sys.exit("The DeerFlow config must route its one model through the gateway.")
    config["sandbox"]["mounts"] = [{{"host_path": workdir, "container_path": workdir, "read_only": False}}]
    root = Path(os.environ["DEER_FLOW_PROJECT_ROOT"])
    root.mkdir(parents=True, exist_ok=True)
    (root / "config.yaml").write_text(json.dumps(config, indent=2), encoding="utf-8")


def _run():
    \"\"\"Stream one DeerFlow turn and print one Pi-shaped ``message_end`` event.\"\"\"
    if not os.environ.get({env_api_key!r}):
        sys.exit("The DeerFlow harness needs the gateway token.")
    for name in DIRECT_ROUTES:
        os.environ.pop(name, None)
    workdir = str(Path.cwd().resolve())
    _write_config(workdir)
    task = Path(os.environ[{env_prompt_file!r}]).read_text(encoding="utf-8").strip()
    # DeerFlow reads config.yaml when the package loads, so it loads after the write.
    client_module = importlib.import_module("deerflow.client")
    errors = importlib.import_module("langgraph.errors")
    client = client_module.DeerFlowClient(
        model_name={model_name!r},
        subagent_enabled=False,
        # The model entry has no ``supports_thinking`` or thinking settings, so DeerFlow
        # sends no thinking parameters either way. Off by default matches that config.
        thinking_enabled=_flag({env_thinking!r}, False),
        plan_mode=False,
    )
    end_usage = None
    message_usage = {{}}
    # DeerFlow streams AI text as deltas that share one message id, so the
    # answer is the joined text of the last AI message, as upstream chat() does.
    texts = {{}}
    last_text = None
    llm_error = None
    try:
        for event in client.stream(STEERING.format(workdir=workdir, task=task), recursion_limit={recursion_limit}):
            data = event.data if isinstance(event.data, dict) else {{}}
            if event.type == "end" and isinstance(data.get("usage"), dict):
                end_usage = data["usage"]
            elif event.type == "messages-tuple" and data.get("type") == "ai":
                # Chunks of one message repeat its id, and id-less chunks share
                # one key, so usage counts once per message and deltas join.
                message_id = data.get("id") or ""
                extra = data.get("additional_kwargs")
                # DeerFlow's LLMErrorHandlingMiddleware replaces a failed provider call
                # with this flagged message; Harbor's runner treats it as a failed run.
                if isinstance(extra, dict) and extra.get("deerflow_error_fallback"):
                    llm_error = str(extra.get("error_detail") or _text(data.get("content")) or "LLM provider failed")
                if isinstance(data.get("usage_metadata"), dict):
                    message_usage[message_id] = data["usage_metadata"]
                piece = _text(data.get("content"))
                if piece:
                    last_text = message_id
                    texts[last_text] = texts.get(last_text, "") + piece
    except errors.GraphRecursionError as exc:
        print(f"deerflow: recursion limit reached: {{exc}}", file=sys.stderr)
    if llm_error is not None:
        sys.exit(f"deerflow: model provider error: {{llm_error[:500]}}")
    answer = texts.get(last_text, "")
    usages = [end_usage] if end_usage is not None else list(message_usage.values())
    event = {{
        "type": "message_end",
        "message": {{
            "role": "assistant",
            "content": [{{"type": "text", "text": answer}}],
            "usage": {{
                "input": sum(_count(usage, "input_tokens") for usage in usages),
                "output": sum(_count(usage, "output_tokens") for usage in usages),
            }},
        }},
    }}
    print(json.dumps(event), flush=True)


if __name__ == "__main__":
    _run()
"""


def _gateway_url(gateway: GatewayConfig) -> str:
    """Return the gateway base URL DeerFlow must call, or refuse the launch.

    Harbor falls back to direct provider keys; Skynet never does, so a missing
    or malformed gateway URL stops the launch here.

    Args:
        gateway: Gateway URL and key.

    Returns:
        The validated gateway URL.

    Raises:
        ServiceError: When the URL is empty, not absolute http(s), embeds
            credentials, or has non-ASCII or other characters that cannot
            sit in the config text.
    """
    url = gateway.url
    if not url or not url.isascii() or any(char.isspace() or char in _URL_FORBIDDEN for char in url):
        raise ServiceError("The DeerFlow harness needs an absolute http(s) gateway URL.")
    parts = urlsplit(url)
    try:
        port_ok = parts.port is None or parts.port > 0
    except ValueError:
        port_ok = False
    if parts.scheme not in ("http", "https") or not parts.hostname or not port_ok or parts.fragment:
        raise ServiceError("The DeerFlow harness needs an absolute http(s) gateway URL.")
    if parts.username or parts.password:
        raise ServiceError("The DeerFlow gateway URL must not embed credentials.")
    return url


def _deerflow_config(model: str, gateway_url: str) -> str:
    """Return DeerFlow's config with one gateway model and the file and bash tools.

    The URL sits in the file text, not the env, so the protected relay rewrite
    of generated files points it at the sandbox-local mailbox. The runner adds
    the workdir mount, because only it knows the absolute workdir.

    Args:
        model: Target model id as the gateway knows it.
        gateway_url: Validated gateway base URL.

    Returns:
        The config as JSON text.
    """
    config = {
        "config_version": 15,
        "log_level": "info",
        "tool_groups": [{"name": group} for group in ("file:read", "file:write", "bash")],
        "tools": [
            {"name": name, "group": group, "use": f"deerflow.sandbox.tools:{tool}"} for name, group, tool in _TOOLS
        ],
        "models": [
            {
                "name": MODEL_NAME,
                "use": "langchain_openai:ChatOpenAI",
                "model": model,
                "base_url": gateway_url,
                # DeerFlow resolves ``$NAME`` from the env, so the token never sits in a file.
                "api_key": f"${ENV_API_KEY}",
                "request_timeout": 600.0,
                "max_retries": 2,
            }
        ],
        "sandbox": {"use": "deerflow.sandbox.local:LocalSandboxProvider", "allow_host_bash": True, "mounts": []},
    }
    return json.dumps(config, indent=2)


def _runner_script(gateway_url: str) -> str:
    """Return the runner that drives ``DeerFlowClient`` and prints a Pi-shaped result.

    The runner holds the gateway URL too, so the protected relay rewrite moves
    it with the config and the runner refuses a config with any other route.

    Args:
        gateway_url: Validated gateway base URL.

    Returns:
        The script source.
    """
    return _RUNNER_SCRIPT.format(
        config_file=CONFIG_FILE,
        gateway_url=gateway_url,
        direct_routes=tuple(_NO_DIRECT_KEYS),
        env_api_key=ENV_API_KEY,
        env_prompt_file=ENV_PROMPT_FILE,
        env_thinking=ENV_THINKING,
        model_name=MODEL_NAME,
        recursion_limit=RECURSION_LIMIT,
    )


def _deerflow_install() -> str:
    """Return the command that installs the pinned DeerFlow source into its own venv.

    Returns:
        The shell command.
    """
    return (
        f"export {_UV_PATH}; {_version_check(_VERSION_BINARY, DEERFLOW_REF)} || {{ "
        "{ command -v uv > /dev/null 2>&1 || "
        f"python3 -m pip install --disable-pip-version-check --no-deps --user {_UV_PIN}; }} && "
        f'rm -rf "{_REPO_DIR}" "{_VENV_DIR}" && mkdir -p "{_REPO_DIR}" && '
        f'git -C "{_REPO_DIR}" init --quiet && '
        f'git -C "{_REPO_DIR}" fetch --quiet --depth 1 {DEERFLOW_REPO} {DEERFLOW_REF} && '
        f'git -C "{_REPO_DIR}" checkout --quiet FETCH_HEAD && '
        f'uv venv --quiet "{_VENV_DIR}" --python {_PYTHON} && '
        f'uv pip install --quiet --python "{_VENV_PYTHON}" "{_REPO_DIR}/backend/packages/harness" && '
        f'printf "%s\\n" {DEERFLOW_REF} > "{_VENV_DIR}/{_REF_MARKER}"; }}'
    )


def _deerflow_launch(model: str, gateway: GatewayConfig, *, reasoning_effort: str | None = None) -> HarnessLaunch:
    """Build the launch for DeerFlow (``deerflow-harness``) at the pinned source commit.

    Args:
        model: Target model id as the gateway knows it.
        gateway: Gateway URL and key; DeerFlow sends every model call there.
        reasoning_effort: The user's effort for the model; ``none`` turns thinking off.

    Returns:
        The launch.
    """
    env = _base_env(model, gateway)
    env.update(_NO_DIRECT_KEYS)
    env.update(_NO_TRACING)
    env["UV_NO_PROGRESS"] = "1"
    gateway_url = _gateway_url(gateway)
    if reasoning_effort:
        env[ENV_THINKING] = "false" if reasoning_effort.lower() == "none" else "true"
    return HarnessLaunch(
        instructions_file="AGENTS.md",
        install_command=_deerflow_install(),
        files={
            CONFIG_FILE: _deerflow_config(model, gateway_url),
            RUNNER_FILE: _runner_script(gateway_url),
        },
        run_command=_RUN_COMMAND,
        env=env,
        parse_output=parse_pi_output,
    )


SPEC = register(
    HarnessSpec(
        id=HARNESS_ID,
        launch=_deerflow_launch,
        binary=_VERSION_BINARY,
        version=DEERFLOW_REF,
        output_format="pi",
        protected_env=_PROTECTED_ENV,
        position=68,
    )
)
