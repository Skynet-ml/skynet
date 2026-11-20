"""Tests for the OpenHands SDK harness: gateway-only launch, pins, protected mode and output parsing."""

from __future__ import annotations

import json
import os
import subprocess
import sys
from pathlib import Path

import pytest

from core.exceptions import ServiceError

from ..harness import GatewayConfig
from ..harness_bridge import parse_pi_output
from ..harnesses import REGISTRY
from ..harnesses.base import _version_check
from ..harnesses.openhands_sdk import (
    EFFORTS,
    ENV_REASONING_EFFORT,
    HARNESS_ID,
    OPENHANDS_SDK_VERSION,
    RUNNER_FILE,
    SPEC,
)

_GATEWAY = GatewayConfig(url="https://gw.example/v1", api_key="secret-key")
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
    def __init__(self, llm, tools, agent_context, mcp_config=None):
        self.llm = llm
        self.tools = tools
        self.agent_context = agent_context
        self.mcp_config = mcp_config


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
            "mcp_config": self.agent.mcp_config,
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

    assert launch.env["ASCENT_MODEL"] == "claude-sonnet-4-5"
    assert launch.env["ASCENT_MODEL_URL"] == _GATEWAY.url
    assert launch.env["ASCENT_MODEL_KEY"] == _GATEWAY.api_key
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


@pytest.mark.parametrize("effort", ["none", "minimal", "low", "medium", "high", "xhigh", "max"])
def test_supported_effort_passes_to_the_sdk_unchanged(effort: str) -> None:
    """Each level the pinned SDK documents reaches the runner exactly as the user chose it."""
    assert effort in EFFORTS
    assert SPEC.launch("m", _GATEWAY, reasoning_effort=effort).env[ENV_REASONING_EFFORT] == effort


def test_spec_declares_the_levels_the_launch_accepts() -> None:
    """Submit and the launch check one constant, so both refuse the same levels."""
    assert SPEC.supported_efforts == EFFORTS == ("none", "minimal", "low", "medium", "high", "xhigh", "max")


@pytest.mark.parametrize("effort", ["minimal", "xhigh", "max"])
def test_runner_sends_the_effort_in_the_extra_body_for_gpt5_ids(tmp_path: Path, effort: str) -> None:
    """The runner puts the effort in LiteLLM's extra body too, so a ``gpt-5`` id cannot lose it.

    Args:
        tmp_path: Test directory.
        effort: A level that LiteLLM's GPT-5 config can drop.
    """
    launch = SPEC.launch("gpt-5.1", _GATEWAY, reasoning_effort=effort)
    prompt = tmp_path / "prompt.md"
    prompt.write_text("Fix the bug.\n")
    record = tmp_path / "record.json"

    result = _run_runner(
        tmp_path, launch.files, **{**launch.env, "ASCENT_TASK": str(prompt), "FAKE_SDK_RECORD": str(record)}
    )

    assert result.returncode == 0, result.stderr
    llm = json.loads(record.read_text())["llm"]
    assert llm["reasoning_effort"] == effort
    assert llm["litellm_extra_body"] == {"reasoning_effort": effort}


def test_runner_sends_no_effort_without_a_choice(tmp_path: Path) -> None:
    """Without a chosen effort the runner sets neither field, so the SDK keeps its default."""
    launch = SPEC.launch("gpt-5.1", _GATEWAY)
    prompt = tmp_path / "prompt.md"
    prompt.write_text("Fix the bug.\n")
    record = tmp_path / "record.json"

    result = _run_runner(
        tmp_path, launch.files, **{**launch.env, "ASCENT_TASK": str(prompt), "FAKE_SDK_RECORD": str(record)}
    )

    assert result.returncode == 0, result.stderr
    llm = json.loads(record.read_text())["llm"]
    assert "reasoning_effort" not in llm
    assert "litellm_extra_body" not in llm


@pytest.mark.parametrize("effort", ["turbo", "auto", "HIGH", "High", " high", "off"])
def test_unsupported_effort_is_refused(effort: str) -> None:
    """A level the SDK does not support stops the launch instead of a silent drop or remap."""
    with pytest.raises(ServiceError, match="does not support the effort level"):
        SPEC.launch("m", _GATEWAY, reasoning_effort=effort)


@pytest.mark.parametrize("effort", [None, ""])
def test_no_effort_keeps_the_sdk_default(effort: str | None) -> None:
    """No effort sets nothing, so the SDK keeps its own default."""
    assert ENV_REASONING_EFFORT not in SPEC.launch("m", _GATEWAY, reasoning_effort=effort).env


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
            "ASCENT_TASK": str(prompt),
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
        "litellm_extra_body": {"reasoning_effort": "high"},
    }
    assert seen["env"] == {
        "LLM_API_KEY": _GATEWAY.api_key,
        "LLM_BASE_URL": _GATEWAY.url,
        "OPENAI_API_KEY": None,
        "ANTHROPIC_API_KEY": None,
    }
    assert seen["tools"] == ["terminal", "file_editor", "task_tracker"]
    assert seen["mcp_config"] is None
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
        **{**launch.env, "ASCENT_TASK": str(prompt), "FAKE_SDK_RECORD": str(record)},
    )

    assert result.returncode == 0, result.stderr
    seen = json.loads(record.read_text())
    assert seen["system_message_suffix"] == candidate
    assert seen["skills"] == []


def test_runner_refuses_without_the_gateway_token(tmp_path: Path) -> None:
    """The runner exits before any SDK call when the gateway token is missing."""
    launch = SPEC.launch("m", _GATEWAY)
    record = tmp_path / "record.json"

    result = _run_runner(
        tmp_path, launch.files, **{**launch.env, "ASCENT_MODEL_KEY": "", "FAKE_SDK_RECORD": str(record)}
    )

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


def test_recorded_output_sample_parses_to_the_final_answer_and_usage() -> None:
    """The Pi parser reads the runner's recorded stream and ignores the non-JSON noise."""
    assert parse_pi_output(_RECORDED_STDOUT) == (
        "Fixed the off-by-one in pager.py; tests pass.",
        {"input_tokens": 9120, "output_tokens": 733},
    )
