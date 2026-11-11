"""Tests for the OpenHands SDK harness: gateway-only launch, pins, protected mode and output parsing."""

from __future__ import annotations

import base64
import io
import json
import os
import subprocess
import sys
import tarfile
from pathlib import Path

import pytest

from core.exceptions import ServiceError
from core.models.blackbox import BLACKBOX_TARGET_AGENT, BlackboxTarget

from ..harness import GatewayConfig, build_launch, pinned_harness_check
from ..harness_bridge import parse_pi_output
from ..harnesses import REGISTRY
from ..harnesses.base import _version_check
from ..harnesses.openhands_sdk import (
    ENV_REASONING_EFFORT,
    HARNESS_ID,
    OPENHANDS_SDK_VERSION,
    RUNNER_FILE,
    SPEC,
)
from ..native_runtime import _bootstrap_command

_GATEWAY = GatewayConfig(url="https://gw.example/v1", api_key="secret-key")
_RELAY = "http://127.0.0.1:3210/v1"
_DIRECT_KEYS = (
    "ANTHROPIC_API_KEY",
    "OPENAI_API_KEY",
    "GEMINI_API_KEY",
    "GOOGLE_API_KEY",
    "OPENROUTER_API_KEY",
    "LLM_API_KEY",
    "LLM_BASE_URL",
    "LMNR_PROJECT_API_KEY",
    "OTEL_ENDPOINT",
    "OTEL_EXPORTER_OTLP_TRACES_ENDPOINT",
    "OTEL_EXPORTER_OTLP_ENDPOINT",
)
_PINS = ("openhands-sdk==1.61.0", "openhands-tools==1.61.0", "fastapi==0.136.3")
# Synthetic stdout of one runner turn: SDK visualizer noise around the single
# Pi-shaped event the runner prints. No real model, key or account produced it.
_RECORDED_STDOUT = "\n".join(
    [
        "Installed 112 packages in 804ms",
        "Agent Action: TerminalTool(command='pytest -q')",
        json.dumps(
            {
                "type": "message_end",
                "message": {
                    "role": "assistant",
                    "content": [{"type": "text", "text": "Fixed the off-by-one in pager.py; tests pass."}],
                    "usage": {"input": 9120, "output": 733},
                },
            }
        ),
        "",
    ]
)

_FAKE_SDK = {
    "openhands/__init__.py": "",
    "openhands/sdk/__init__.py": """
import json
import os
import tempfile

# The real SDK freezes its user state folder at import time.
_PERSISTENCE_DIR = os.environ.get("OH_PERSISTENCE_DIR")


class _Usage:
    prompt_tokens = 11
    completion_tokens = 7


class _Metrics:
    accumulated_token_usage = _Usage()


class LLM:
    def __init__(self, **kwargs):
        self.kwargs = kwargs
        self.metrics = _Metrics()


class Tool:
    def __init__(self, name):
        self.name = name


class AgentContext:
    def __init__(self, system_message_suffix=None, skills=()):
        self.system_message_suffix = system_message_suffix
        self.skills = list(skills)


class Agent:
    def __init__(self, llm, tools, agent_context):
        self.llm = llm
        self.tools = tools
        self.agent_context = agent_context


class _State:
    def __init__(self):
        self.events = []


class Conversation:
    def __init__(self, agent, workspace, visualizer="default"):
        self.agent = agent
        self.workspace = workspace
        self.visualizer = visualizer
        self.state = _State()

    def send_message(self, prompt):
        from openhands.sdk.event import MessageEvent
        self.state.events.append(MessageEvent("user", prompt))

    def run(self):
        from openhands.sdk.conversation.impl import local_conversation
        from openhands.sdk.event import ActionEvent, MessageEvent
        scratch = tempfile.mkdtemp()
        record = {
            "llm": self.agent.llm.kwargs,
            "env": {name: os.environ.get(name) for name in (
                "LLM_API_KEY", "LLM_BASE_URL", "OPENAI_API_KEY", "ANTHROPIC_API_KEY")},
            "tools": [tool.name for tool in self.agent.tools],
            "skills": self.agent.agent_context.skills,
            "system_message_suffix": self.agent.agent_context.system_message_suffix,
            "visualizer": self.visualizer,
            "banner_off": os.environ.get("OPENHANDS_SUPPRESS_BANNER"),
            "ambient_plugins": local_conversation.load_available_plugins(
                work_dir=self.workspace, include_user=True, include_project=True),
            "workspace": self.workspace,
            "scratch": scratch,
            "persistence_dir": _PERSISTENCE_DIR,
            "persistence_files": os.listdir(_PERSISTENCE_DIR) if _PERSISTENCE_DIR else None,
        }
        with open(os.environ["FAKE_SDK_RECORD"], "w") as handle:
            json.dump(record, handle)
        prompt = self.state.events[0].llm_message.content[0].text
        self.state.events.append(MessageEvent("agent", "working on " + prompt.strip()))
        self.state.events.append(ActionEvent("answer to " + prompt.strip()))
""",
    "openhands/sdk/conversation/__init__.py": "",
    "openhands/sdk/conversation/impl/__init__.py": "",
    "openhands/sdk/conversation/impl/local_conversation.py": """
def load_available_plugins(work_dir, include_user, include_project):
    return {"ambient": "a plugin from the home or workspace plugin folders"}
""",
    "openhands/sdk/event.py": """
class _Part:
    def __init__(self, text):
        self.text = text


class _Message:
    def __init__(self, text):
        self.content = [_Part(text)]


class MessageEvent:
    def __init__(self, source, text):
        self.source = source
        self.llm_message = _Message(text)


class _Finish:
    def __init__(self, message):
        self.message = message


class ActionEvent:
    def __init__(self, message):
        self.action = _Finish(message)
""",
    "openhands/tools/__init__.py": "",
    "openhands/tools/terminal.py": "class TerminalTool:\n    name = 'terminal'\n",
    "openhands/tools/file_editor.py": "class FileEditorTool:\n    name = 'file_editor'\n",
    "openhands/tools/task_tracker.py": "class TaskTrackerTool:\n    name = 'task_tracker'\n",
    "openhands_sdk-1.61.0.dist-info/METADATA": "Metadata-Version: 2.1\nName: openhands-sdk\nVersion: 1.61.0\n",
}


def _fake_sdk(root: Path) -> Path:
    """Write a fake ``openhands`` package so the runner executes without the real SDK.

    Args:
        root: Directory to hold the fake package.

    Returns:
        The directory to put on ``PYTHONPATH``.
    """
    for name, content in _FAKE_SDK.items():
        path = root / name
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(content)
    return root


def _run_runner(tmp_path: Path, launch_files: dict[str, str], **env: str) -> subprocess.CompletedProcess:
    """Run the generated runner with the current Python against the fake SDK.

    Args:
        tmp_path: Test directory; the workspace and fake SDK live under it.
        launch_files: Generated launch files to write into the workspace.
        **env: Extra environment variables.

    Returns:
        The finished process.
    """
    workspace = tmp_path / "ws"
    for name, content in launch_files.items():
        path = workspace / name
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(content)
    return subprocess.run(
        [sys.executable, RUNNER_FILE],
        cwd=workspace,
        env={"PATH": os.defpath, "PYTHONPATH": str(_fake_sdk(tmp_path / "fake")), **env},
        capture_output=True,
        text=True,
        timeout=30,
        check=False,
    )


def test_spec_is_registered_with_a_pinned_version_and_the_pi_parser() -> None:
    """The registry holds the harness with an exact SDK pin, its slot and an existing parser."""
    assert REGISTRY[HARNESS_ID] is SPEC
    assert SPEC.version == OPENHANDS_SDK_VERSION == "1.61.0"
    assert SPEC.output_format == "pi"
    assert SPEC.position == 56
    assert SPEC.launch("m", _GATEWAY).parse_output is parse_pi_output


def test_launch_routes_through_the_gateway_without_a_direct_key() -> None:
    """The env carries the gateway token and blanks every direct provider credential."""
    launch = SPEC.launch("claude-sonnet-4-5", _GATEWAY)

    assert launch.env["APP_MODEL"] == "claude-sonnet-4-5"
    assert launch.env["APP_GATEWAY_URL"] == _GATEWAY.url
    assert launch.env["APP_API_KEY"] == _GATEWAY.api_key
    for name in _DIRECT_KEYS:
        assert launch.env[name] == ""
    assert launch.env["DO_NOT_TRACK"] == "1"
    assert launch.env["OPENHANDS_SUPPRESS_BANNER"] == "1"
    assert ENV_REASONING_EFFORT not in launch.env
    assert list(launch.files) == [RUNNER_FILE]
    assert launch.run_command == f'PATH="$HOME/.local/bin:$PATH" uv run --quiet --script {RUNNER_FILE}'
    assert _GATEWAY.api_key not in launch.run_command
    assert launch.instructions_file == "AGENTS.md"


def test_runner_pins_exact_dependencies_and_embeds_the_gateway_url() -> None:
    """The uv script pins every dependency exactly and holds the gateway URL as a literal."""
    runner = SPEC.launch("m", _GATEWAY).files[RUNNER_FILE]

    compile(runner, RUNNER_FILE, "exec")
    assert runner.startswith("#!/usr/bin/env -S uv run --script\n")
    for pin in _PINS:
        assert f'"{pin}"' in runner
    assert 'requires-python = ">=3.12,<3.15"' in runner
    assert f"GATEWAY_URL = {_GATEWAY.url!r}" in runner
    assert _GATEWAY.api_key not in runner
    assert "Adapted from Harbor 0.25.0 (Apache-2.0)" in runner


def test_install_pins_uv_and_warms_the_sdk_environment() -> None:
    """The open-mode install reuses the repo's uv pin and checks the SDK version first."""
    install = SPEC.launch("m", _GATEWAY).install_command

    assert install is not None
    assert install.startswith('export PATH="$HOME/.local/bin:$PATH"; ')
    assert _version_check(SPEC.binary, OPENHANDS_SDK_VERSION) in install
    assert "uv==0.9.13" in install
    assert "curl" not in install
    assert RUNNER_FILE not in install
    assert install.endswith("""print(m.version("openhands-sdk"))' > /dev/null; }""")


def test_version_probe_needs_no_workspace_file_and_never_downloads() -> None:
    """The probe resolves the exact script pins offline, so it runs before the launch files exist."""
    assert RUNNER_FILE not in SPEC.binary
    assert SPEC.binary.startswith("uv run --quiet --offline --no-project ")
    for pin in _PINS:
        assert f"--with {pin}" in SPEC.binary


@pytest.mark.parametrize(
    ("effort", "expected"),
    [("none", "none"), ("low", "low"), ("high", "high"), ("xhigh", "xhigh"), ("max", "max"), ("HIGH", "high")],
)
def test_reasoning_effort_passes_to_the_sdk(effort: str, expected: str) -> None:
    """Each known Skynet effort reaches the runner in the SDK's lower-case form."""
    assert SPEC.launch("m", _GATEWAY, reasoning_effort=effort).env[ENV_REASONING_EFFORT] == expected


def test_unknown_effort_keeps_the_sdk_default() -> None:
    """An effort LiteLLM does not know sets nothing, so the SDK keeps its default."""
    assert ENV_REASONING_EFFORT not in SPEC.launch("m", _GATEWAY, reasoning_effort="turbo").env


@pytest.mark.parametrize(
    "url",
    [
        "",
        "gw.example/v1",
        "/v1",
        "ftp://gw.example/v1",
        "https://user:pw@gw.example/v1",
        "https://gw.example/v1#frag",
        "https://gw.example:0/v1",
        " https://gw.example/v1",
        "https://gw.example/v1\n",
        'https://gw.example/v1"',
        "https://gw.example/v1\\x",
    ],
)
def test_launch_refuses_without_an_absolute_gateway_url(url: str) -> None:
    """No gateway URL means no launch; the harness never falls back to a direct key."""
    with pytest.raises(ServiceError, match="OpenHands SDK"):
        SPEC.launch("m", GatewayConfig(url=url, api_key="secret-key"))


def test_protected_launch_checks_the_pin_offline_and_rewrites_the_runner_to_the_relay(tmp_path: Path) -> None:
    """Protected mode uses the version probe, offline uv env, and points the runner at the local relay."""
    target = BlackboxTarget.model_construct(
        kind=BLACKBOX_TARGET_AGENT, harness=HARNESS_ID, model="m", run_command="true"
    )
    launch = build_launch(target, _GATEWAY, protected=True)

    assert launch.install_command is not None
    assert launch.install_command == pinned_harness_check(HARNESS_ID)
    assert "pip install" not in launch.install_command
    assert launch.env["UV_OFFLINE"] == "1"
    assert launch.env["DO_NOT_TRACK"] == "1"
    assert launch.env["OPENHANDS_SUPPRESS_BANNER"] == "1"
    for name in _DIRECT_KEYS:
        assert launch.env[name] == ""
    for name, content in launch.files.items():
        path = tmp_path / name
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(content)
    subprocess.run(
        launch.run_command,
        shell=True,
        cwd=tmp_path,
        env={"PATH": os.defpath, "APP_BUDGET_RELAY_URL": _RELAY},
        timeout=10,
        check=True,
    )
    runner = (tmp_path / RUNNER_FILE).read_text()
    assert _GATEWAY.url not in runner
    assert f"GATEWAY_URL = {_RELAY!r}" in runner


@pytest.mark.parametrize(("reported", "ok"), [("1.61.0", True), ("1.61.1", False), ("11.61.0", False)])
def test_pinned_check_accepts_only_the_exact_sdk_version(tmp_path: Path, reported: str, ok: bool) -> None:
    """The offline probe passes only when the warmed script reports the pinned SDK release."""
    bin_dir = tmp_path / "bin"
    bin_dir.mkdir()
    fake_uv = bin_dir / "uv"
    fake_uv.write_text(f"#!/bin/sh\necho {reported}\n")
    fake_uv.chmod(0o755)
    check = pinned_harness_check(HARNESS_ID)
    assert check is not None

    result = subprocess.run(
        check,
        shell=True,
        cwd=tmp_path,
        env={"PATH": f"{bin_dir}{os.pathsep}{os.defpath}"},
        timeout=10,
        check=False,
    )

    assert (result.returncode == 0) is ok


def test_runner_sends_the_turn_to_the_gateway_and_prints_a_pi_event(tmp_path: Path) -> None:
    """Against a fake SDK, the runner points the LLM at the gateway, drops direct keys and prints parseable output."""
    launch = SPEC.launch("claude-sonnet-4-5", _GATEWAY, reasoning_effort="high")
    prompt = tmp_path / "prompt.md"
    prompt.write_text("Fix the bug.\n")
    record = tmp_path / "record.json"

    result = _run_runner(
        tmp_path,
        launch.files,
        **{
            **launch.env,
            "APP_PROMPT_FILE": str(prompt),
            "OPENAI_API_KEY": "leaked-direct-key",
            "ANTHROPIC_API_KEY": "leaked-direct-key",
            "FAKE_SDK_RECORD": str(record),
        },
    )

    assert result.returncode == 0, result.stderr
    seen = json.loads(record.read_text())
    workspace = (tmp_path / "ws").resolve()
    assert seen["llm"] == {
        "model": "openai/claude-sonnet-4-5",
        "base_url": _GATEWAY.url,
        "api_key": _GATEWAY.api_key,
        "usage_id": "agent",
        "api_mode": "chat",
        "reasoning_effort": "high",
    }
    assert seen["env"] == {
        "LLM_API_KEY": _GATEWAY.api_key,
        "LLM_BASE_URL": _GATEWAY.url,
        "OPENAI_API_KEY": None,
        "ANTHROPIC_API_KEY": None,
    }
    assert seen["tools"] == ["terminal", "file_editor", "task_tracker"]
    assert seen["skills"] == []
    assert seen["system_message_suffix"] is None
    assert seen["visualizer"] is None
    assert seen["banner_off"] == "1"
    assert seen["ambient_plugins"] == {}
    assert seen["workspace"] == str(workspace)
    assert Path(seen["scratch"]).resolve().is_relative_to(workspace / ".app/openhands_sdk")
    assert not Path(seen["scratch"]).exists()
    assert Path(seen["persistence_dir"]).is_relative_to(workspace / ".app/openhands_sdk")
    assert seen["persistence_files"] == []
    assert not Path(seen["persistence_dir"]).exists()
    assert list((workspace / ".app/openhands_sdk").iterdir()) == [workspace / RUNNER_FILE]
    assert parse_pi_output(result.stdout) == ("answer to Fix the bug.", {"input_tokens": 11, "output_tokens": 7})
    assert result.stdout.strip().count("\n") == 0


def test_runner_gives_the_candidate_instructions_to_the_agent(tmp_path: Path) -> None:
    """The AGENTS.md candidate reaches the system prompt as raw suffix text, not as an untrusted repo skill."""
    launch = SPEC.launch("m", _GATEWAY)
    prompt = tmp_path / "prompt.md"
    prompt.write_text("Fix the bug.\n")
    record = tmp_path / "record.json"
    candidate = "Always run the tests before you finish. marker-7f3a"

    result = _run_runner(
        tmp_path,
        {**launch.files, launch.instructions_file: candidate},
        **{**launch.env, "APP_PROMPT_FILE": str(prompt), "FAKE_SDK_RECORD": str(record)},
    )

    assert result.returncode == 0, result.stderr
    seen = json.loads(record.read_text())
    assert seen["system_message_suffix"] == candidate
    assert seen["skills"] == []


def test_runner_refuses_without_the_gateway_token(tmp_path: Path) -> None:
    """The runner exits before any SDK call when the gateway token is missing."""
    launch = SPEC.launch("m", _GATEWAY)
    record = tmp_path / "record.json"

    result = _run_runner(tmp_path, launch.files, **{**launch.env, "APP_API_KEY": "", "FAKE_SDK_RECORD": str(record)})

    assert result.returncode != 0
    assert "gateway" in result.stderr
    assert not record.exists()
    assert list((tmp_path / "ws/.app/openhands_sdk").iterdir()) == [tmp_path / "ws" / RUNNER_FILE]


# A fake uv that refuses the workspace script, needs the exact pins, and
# answers an offline run only from a warmed cache (the marker file).
_FAKE_UV = """#!/bin/sh
case " $* " in *" --script "*) echo "no such script" >&2; exit 2;; esac
case " $* " in *" --no-project "*) ;; *) exit 3;; esac
case " $* " in *" --with openhands-sdk==1.61.0 "*) ;; *) exit 4;; esac
case " $* " in
  *" --offline "*) test -f "$FAKE_UV_CACHE" || exit 5;;
  *) : > "$FAKE_UV_CACHE";;
esac
echo 1.61.0
"""


def _native_workspace(tmp_path: Path) -> tuple[Path, Path]:
    """Write the fake uv, node and python3 tools and an empty workspace with the source archive.

    Args:
        tmp_path: Test directory.

    Returns:
        ``(bin_dir, workspace)``: the tool directory for ``PATH`` and the bootstrap's working directory.
    """
    bin_dir = tmp_path / "bin"
    bin_dir.mkdir()
    for name, content in {
        "uv": _FAKE_UV,
        "node": "#!/bin/sh\nexit 0\n",
        "python3": f'#!/bin/sh\nexec {sys.executable} "$@"\n',
    }.items():
        tool = bin_dir / name
        tool.write_text(content)
        tool.chmod(0o755)
    workspace = tmp_path / "ws"
    workspace.mkdir()
    archive = io.BytesIO()
    with tarfile.open(fileobj=archive, mode="w:gz"):
        pass
    (workspace / "native_source.tar.gz.b64").write_text(base64.b64encode(archive.getvalue()).decode())
    return bin_dir, workspace


def _run_native_bootstrap(tmp_path: Path, *, protected: bool, warmed: bool) -> tuple[subprocess.CompletedProcess, Path]:
    """Run the native proposer bootstrap for this harness against the fake tools.

    Args:
        tmp_path: Test directory.
        protected: Whether to build the protected (offline image) bootstrap.
        warmed: Whether the fake uv cache already holds the pinned SDK.

    Returns:
        The finished process and the fake uv cache marker.
    """
    target = BlackboxTarget.model_construct(
        kind=BLACKBOX_TARGET_AGENT, harness=HARNESS_ID, model="m", run_command="true"
    )
    launch = build_launch(target, _GATEWAY, protected=protected)
    bin_dir, workspace = _native_workspace(tmp_path)
    cache = tmp_path / "uv-cache"
    if warmed:
        cache.touch()
    command = _bootstrap_command(
        "vercel", protected=protected, harness=HARNESS_ID, install_command=launch.install_command
    )
    result = subprocess.run(
        ["sh", "-c", command],
        cwd=workspace,
        env={"PATH": f"{bin_dir}{os.pathsep}{os.defpath}", "FAKE_UV_CACHE": str(cache), **launch.env},
        capture_output=True,
        text=True,
        timeout=60,
        check=False,
    )
    assert not (workspace / RUNNER_FILE).exists()
    return result, cache


@pytest.mark.parametrize("protected", [False, True])
def test_native_bootstrap_passes_before_the_launch_files_exist(tmp_path: Path, protected: bool) -> None:
    """The native proposer bootstrap installs and checks the harness in a workspace without the runner."""
    result, cache = _run_native_bootstrap(tmp_path, protected=protected, warmed=protected)

    assert result.returncode == 0, result.stderr
    assert cache.exists()


def test_native_bootstrap_fails_when_the_protected_image_lacks_the_sdk(tmp_path: Path) -> None:
    """Protected mode never downloads: an image without the warmed SDK fails the pinned check."""
    result, cache = _run_native_bootstrap(tmp_path, protected=True, warmed=False)

    assert result.returncode != 0
    assert not cache.exists()


def test_recorded_output_sample_parses_to_the_final_answer_and_usage() -> None:
    """The Pi parser reads the runner's recorded stream and ignores the non-JSON noise."""
    assert parse_pi_output(_RECORDED_STDOUT) == (
        "Fixed the off-by-one in pager.py; tests pass.",
        {"input_tokens": 9120, "output_tokens": 733},
    )
