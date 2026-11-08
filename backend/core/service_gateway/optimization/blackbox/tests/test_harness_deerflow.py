"""Tests for the DeerFlow harness: gateway-only launch, pins, protected mode and output parsing."""

from __future__ import annotations

import json
import os
import subprocess
import sys
import venv
from pathlib import Path

import pytest

from core.exceptions import ServiceError
from core.models.blackbox import BLACKBOX_HARNESSES, BLACKBOX_TARGET_AGENT, BlackboxTarget

from ..harness import GatewayConfig, build_launch, pinned_harness_check
from ..harness_bridge import parse_pi_output
from ..harnesses import REGISTRY
from ..harnesses.base import _version_check
from ..harnesses.deerflow import (
    CONFIG_FILE,
    DEERFLOW_PACKAGE,
    DEERFLOW_REF,
    DEERFLOW_REPO,
    ENV_THINKING,
    HARNESS_ID,
    MODEL_NAME,
    RUNNER_FILE,
    SPEC,
)

_GATEWAY = GatewayConfig(url="https://gw.example/v1", api_key="secret-key")
_RELAY = "http://127.0.0.1:3210/v1"
_PROVIDER_KEYS = ("OPENAI_API_KEY", "ANTHROPIC_API_KEY", "GEMINI_API_KEY", "DEEPSEEK_API_KEY", "OPENROUTER_API_KEY")
_SEARCH_KEYS = ("TAVILY_API_KEY", "SERPER_API_KEY", "EXA_API_KEY", "FIRECRAWL_API_KEY", "JINA_API_KEY")
_TRACING_KEYS = ("LANGSMITH_API_KEY", "LANGFUSE_PUBLIC_KEY", "LANGFUSE_SECRET_KEY")
# Synthetic stdout of one runner turn: DeerFlow log noise around the single
# Pi-shaped event the runner prints. No real model, key or account produced it.
_RECORDED_STDOUT = "\n".join(
    [
        "INFO deerflow.config: loaded config.yaml",
        "not json at all",
        json.dumps(
            {
                "type": "message_end",
                "message": {
                    "role": "assistant",
                    "content": [{"type": "text", "text": "Fixed the off-by-one in pager.py; tests pass."}],
                    "usage": {"input": 2210, "output": 388},
                },
            }
        ),
        "",
    ]
)

_FAKE_DEERFLOW = {
    "deerflow/__init__.py": "",
    "deerflow/client.py": """
import json
import os
from pathlib import Path
from types import SimpleNamespace


class DeerFlowClient:
    def __init__(self, **kwargs):
        self.kwargs = kwargs

    def stream(self, message, recursion_limit):
        config = Path(os.environ["DEER_FLOW_PROJECT_ROOT"], "config.yaml").read_text()
        record = {
            "kwargs": self.kwargs,
            "message": message,
            "recursion_limit": recursion_limit,
            "config": json.loads(config),
            "keys": {name: os.environ.get(name) for name in ("OPENAI_API_KEY", "EXA_API_KEY")},
        }
        Path(os.environ["FAKE_DEERFLOW_RECORD"]).write_text(json.dumps(record))
        yield SimpleNamespace(type="values", data={"title": "x"})
        yield SimpleNamespace(
            type="messages-tuple",
            data={"type": "ai", "id": "m1", "content": "draft", "usage_metadata": {"input_tokens": 3}},
        )
        yield SimpleNamespace(
            type="messages-tuple",
            data={"type": "ai", "id": "m2", "content": [{"type": "text", "text": "final answer"}]},
        )
        yield SimpleNamespace(type="messages-tuple", data={"type": "tool", "id": "t1", "content": "tool noise"})
        yield SimpleNamespace(type="end", data={"usage": {"input_tokens": 40, "output_tokens": 9}})
""",
    "langgraph/__init__.py": "",
    "langgraph/errors.py": "class GraphRecursionError(Exception):\n    pass\n",
}


def _target(**extra: str) -> BlackboxTarget:
    """Return an agent target on the DeerFlow harness.

    Args:
        **extra: Target field overrides.

    Returns:
        The target.
    """
    return BlackboxTarget(kind=BLACKBOX_TARGET_AGENT, harness=HARNESS_ID, model="openrouter/acme/agent-1", **extra)


def test_registry_lists_deerflow_with_its_pin() -> None:
    """Register DeerFlow last among built-ins, pinned to its exact source commit."""
    assert REGISTRY[HARNESS_ID] is SPEC
    assert HARNESS_ID in BLACKBOX_HARNESSES
    assert SPEC.output_format == "pi"
    assert SPEC.version == DEERFLOW_REF
    assert len(DEERFLOW_REF) == 40
    assert pinned_harness_check(HARNESS_ID) == _version_check(SPEC.binary, DEERFLOW_REF)


def test_launch_runs_the_runner_on_the_gateway_only() -> None:
    """Send every model call to the gateway and blank every direct, search and tracing key."""
    launch = build_launch(_target(), _GATEWAY)
    assert launch.parse_output is parse_pi_output
    assert launch.run_command == (
        'DEER_FLOW_PROJECT_ROOT="$PWD/.app/deerflow/project" DEER_FLOW_HOME="$PWD/.app/deerflow/home" '
        f'"$HOME/.app/deerflow/venv/bin/python" {RUNNER_FILE}'
    )
    assert set(launch.files) == {CONFIG_FILE, RUNNER_FILE}
    config = json.loads(launch.files[CONFIG_FILE])
    assert config["models"] == [
        {
            "name": MODEL_NAME,
            "use": "langchain_openai:ChatOpenAI",
            "model": "acme/agent-1",
            "base_url": "https://gw.example/v1",
            "api_key": "$APP_API_KEY",
            "request_timeout": 600.0,
            "max_retries": 2,
        }
    ]
    assert [group["name"] for group in config["tool_groups"]] == ["file:read", "file:write", "bash"]
    assert all(tool["use"].startswith("deerflow.sandbox.tools:") for tool in config["tools"])
    assert config["sandbox"]["use"] == "deerflow.sandbox.local:LocalSandboxProvider"
    assert "secret-key" not in launch.files[CONFIG_FILE] + launch.files[RUNNER_FILE]
    compile(launch.files[RUNNER_FILE], RUNNER_FILE, "exec")
    env = launch.env
    assert env["APP_API_KEY"] == "secret-key"
    assert env["APP_MODEL"] == "acme/agent-1"
    assert all(env[name] == "" for name in (*_PROVIDER_KEYS, *_SEARCH_KEYS, *_TRACING_KEYS))
    assert env["LANGSMITH_TRACING"] == "false"
    assert env["LANGCHAIN_TRACING_V2"] == "false"
    assert env["DO_NOT_TRACK"] == "1"
    assert ENV_THINKING not in env


def test_install_clones_the_pinned_commit_into_a_venv() -> None:
    """Install DeerFlow from its exact commit and record that commit for the version probe."""
    install = build_launch(_target(), _GATEWAY).install_command
    assert install.startswith(f'export PATH="$HOME/.local/bin:$PATH"; {_version_check(SPEC.binary, DEERFLOW_REF)} || ')
    assert f"fetch --quiet --depth 1 {DEERFLOW_REPO} {DEERFLOW_REF}" in install
    assert "backend/packages/harness" in install
    assert "uv==0.9.13" in install
    assert f'printf "%s\\n" {DEERFLOW_REF} > "$HOME/.app/deerflow/venv/deerflow-ref"' in install
    assert "astral.sh" not in install


@pytest.mark.parametrize(("effort", "expected"), [("none", "false"), ("NONE", "false"), ("high", "true")])
def test_effort_switches_thinking(effort: str, expected: str) -> None:
    """Turn DeerFlow thinking off only for the ``none`` effort.

    Args:
        effort: Reasoning effort the user picked.
        expected: The thinking toggle the runner gets.
    """
    assert build_launch(_target(), _GATEWAY, reasoning_effort=effort).env[ENV_THINKING] == expected


def test_protected_launch_uses_the_pin_relay_and_offline_env(tmp_path: Path) -> None:
    """Probe the preinstalled pin, point the config at the relay, and stay offline.

    Args:
        tmp_path: Workspace the protected run command rewrites.
    """
    launch = build_launch(_target(), _GATEWAY, protected=True)
    assert launch.install_command == pinned_harness_check(HARNESS_ID)
    env = launch.env
    assert env["UV_OFFLINE"] == "1"
    assert env["DO_NOT_TRACK"] == "1"
    assert all(env[name] == "" for name in (*_PROVIDER_KEYS, *_SEARCH_KEYS, *_TRACING_KEYS))
    for name, source in launch.files.items():
        (tmp_path / name).parent.mkdir(parents=True, exist_ok=True)
        (tmp_path / name).write_text(source)
    # The runner is missing from the fake home, so the command fails after the rewrite.
    subprocess.run(
        ["bash", "-c", launch.run_command],
        cwd=tmp_path,
        env={"PATH": os.environ.get("PATH", ""), "HOME": str(tmp_path), "APP_BUDGET_RELAY_URL": _RELAY},
        capture_output=True,
        timeout=60,
        check=False,
    )
    config = json.loads((tmp_path / CONFIG_FILE).read_text())
    assert config["models"][0]["base_url"] == _RELAY
    assert all(_GATEWAY.url not in (tmp_path / name).read_text() for name in launch.files)
    assert repr(_RELAY) in (tmp_path / RUNNER_FILE).read_text()


@pytest.mark.parametrize(
    "url",
    [
        "",
        "gw.example/v1",
        "ftp://gw.example/v1",
        "https://user:pw@gw.example/v1",
        "https://gw.example/v1'x",
        "https://",
    ],
)
def test_bad_gateway_url_refuses_the_launch(url: str) -> None:
    """Refuse a gateway URL that would let DeerFlow fall back to another route.

    Args:
        url: Malformed gateway URL.
    """
    with pytest.raises(ServiceError):
        build_launch(_target(), GatewayConfig(url=url, api_key="secret-key"))


def test_parser_reads_the_recorded_runner_output() -> None:
    """Turn the recorded runner stdout into the answer and token counts."""
    assert parse_pi_output(_RECORDED_STDOUT) == (
        "Fixed the off-by-one in pager.py; tests pass.",
        {"input_tokens": 2210, "output_tokens": 388},
    )


def test_runner_drives_the_client_and_prints_one_pi_event(tmp_path: Path) -> None:
    """Run the shipped runner against a fake DeerFlow and parse what it prints.

    Args:
        tmp_path: Workspace and fake package root.
    """
    launch = build_launch(_target(), _GATEWAY, reasoning_effort="none")
    fakes = tmp_path / "fakes"
    for name, source in _FAKE_DEERFLOW.items():
        (fakes / name).parent.mkdir(parents=True, exist_ok=True)
        (fakes / name).write_text(source)
    workspace = tmp_path / "work"
    for name, source in launch.files.items():
        (workspace / name).parent.mkdir(parents=True, exist_ok=True)
        (workspace / name).write_text(source)
    (workspace / "task").mkdir()
    (workspace / "task" / "PROMPT.md").write_text("Fix the pager.\n")
    record = tmp_path / "record.json"
    env = {
        "PATH": os.environ.get("PATH", ""),
        "PYTHONPATH": str(fakes),
        **launch.env,
        "APP_PROMPT_FILE": "task/PROMPT.md",
        "OPENAI_API_KEY": "leaked-host-key",
        "DEER_FLOW_PROJECT_ROOT": str(workspace / ".app/deerflow/project"),
        "FAKE_DEERFLOW_RECORD": str(record),
    }
    completed = subprocess.run(
        [sys.executable, RUNNER_FILE], cwd=workspace, env=env, capture_output=True, text=True, timeout=60, check=False
    )
    assert completed.returncode == 0, completed.stderr
    assert parse_pi_output(completed.stdout) == ("final answer", {"input_tokens": 40, "output_tokens": 9})
    seen = json.loads(record.read_text())
    assert seen["kwargs"] == {
        "model_name": MODEL_NAME,
        "subagent_enabled": False,
        "thinking_enabled": False,
        "plan_mode": False,
    }
    assert seen["recursion_limit"] == 1000
    assert seen["message"].endswith("Task:\nFix the pager.")
    assert str(workspace.resolve()) in seen["message"]
    assert seen["keys"] == {"OPENAI_API_KEY": None, "EXA_API_KEY": None}
    assert seen["config"]["models"][0]["base_url"] == _GATEWAY.url
    mount = {"host_path": str(workspace.resolve()), "container_path": str(workspace.resolve()), "read_only": False}
    assert seen["config"]["sandbox"]["mounts"] == [mount]


def test_runner_refuses_a_config_with_another_model_route(tmp_path: Path) -> None:
    """Stop before DeerFlow loads when the config routes the model off the gateway.

    Args:
        tmp_path: Workspace with a tampered config.
    """
    launch = build_launch(_target(), _GATEWAY)
    for name, source in launch.files.items():
        (tmp_path / name).parent.mkdir(parents=True, exist_ok=True)
        (tmp_path / name).write_text(source.replace(_GATEWAY.url, "https://api.openai.com/v1"))
    (tmp_path / RUNNER_FILE).write_text(launch.files[RUNNER_FILE])
    completed = subprocess.run(
        [sys.executable, RUNNER_FILE],
        cwd=tmp_path,
        env={**launch.env, "PATH": os.environ.get("PATH", ""), "DEER_FLOW_PROJECT_ROOT": str(tmp_path / "project")},
        capture_output=True,
        text=True,
        timeout=60,
        check=False,
    )
    assert completed.returncode == 1
    assert "through the gateway" in completed.stderr
    assert not (tmp_path / "project").exists()


def _fake_venv(home: Path, marker: str) -> None:
    """Create the DeerFlow venv layout with a fake installed package and a ref marker.

    Args:
        home: Fake ``$HOME``.
        marker: Commit the install recorded.
    """
    root = home / ".app/deerflow/venv"
    venv.create(root, with_pip=False, symlinks=True)
    site = next(root.glob("lib/python*/site-packages"))
    dist = site / "deerflow_harness-0.1.0.dist-info"
    dist.mkdir()
    (dist / "METADATA").write_text(f"Metadata-Version: 2.1\nName: {DEERFLOW_PACKAGE}\nVersion: 0.1.0\n")
    (root / "deerflow-ref").write_text(marker + "\n")


@pytest.mark.parametrize(("marker", "code"), [(DEERFLOW_REF, 0), ("0" * 40, 1)])
def test_version_probe_accepts_only_the_pinned_commit(tmp_path: Path, marker: str, code: int) -> None:
    """Pass the pin probe only when the venv records the exact pinned commit.

    Args:
        tmp_path: Fake home directory.
        marker: Commit the fake install recorded.
        code: Expected probe exit status.
    """
    _fake_venv(tmp_path, marker)
    completed = subprocess.run(
        ["bash", "-c", pinned_harness_check(HARNESS_ID)],
        cwd=tmp_path,
        env={"PATH": os.environ.get("PATH", ""), "HOME": str(tmp_path)},
        timeout=60,
        check=False,
    )
    assert completed.returncode == code
