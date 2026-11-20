"""Tests for the Google Antigravity SDK harness: gateway-only launch, pins, protected mode and output parsing."""

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
from ..harnesses.antigravity_sdk import (
    ANTIGRAVITY_SDK_VERSION,
    ENV_THINKING_LEVEL,
    HARNESS_ID,
    RUNNER_FILE,
    SPEC,
)
from ..harnesses.base import _version_check

_GATEWAY = GatewayConfig(url="https://gw.example/v1", api_key="secret-key")
_DIRECT_KEYS = ("GEMINI_API_KEY", "GOOGLE_API_KEY", "GOOGLE_APPLICATION_CREDENTIALS")
# Synthetic stdout of one runner turn: uv and SDK noise around the single
# Pi-shaped event the runner prints. No real model, key or account produced it.
_RECORDED_STDOUT = "\n".join(
    [
        "Installed 47 packages in 312ms",
        "not json at all",
        json.dumps(
            {
                "type": "message_end",
                "message": {
                    "role": "assistant",
                    "content": [{"type": "text", "text": "Patched parse_date and the tests pass."}],
                    "usage": {"input": 1834, "output": 421},
                },
            }
        ),
        "",
    ]
)

_FAKE_SDK = {
    "google/__init__.py": "",
    "google/antigravity/__init__.py": """
import json
import os


class LocalAgentConfig:
    def __init__(self, **kwargs):
        self.kwargs = kwargs
        self.eval_kwargs = None

    def eval(self, **kwargs):
        self.eval_kwargs = kwargs
        return self


class _Usage:
    prompt_token_count = 11
    candidates_token_count = 5
    thoughts_token_count = 2


class _Conversation:
    total_usage = _Usage()


class _Response:
    def __init__(self, prompt):
        self.prompt = prompt

    async def text(self):
        return "answer to " + self.prompt.strip()

    async def resolve(self):
        return None


class Agent:
    def __init__(self, config):
        self.config = config
        self.conversation = _Conversation()

    async def __aenter__(self):
        return self

    async def __aexit__(self, *exc):
        return False

    async def chat(self, prompt):
        target = self.config.kwargs["model"]
        record = {
            "base_url": os.environ.get("GOOGLE_GEMINI_BASE_URL"),
            "direct_keys": {name: os.environ.get(name) for name in ("GEMINI_API_KEY", "GOOGLE_API_KEY")},
            "api_key": self.config.kwargs["api_key"],
            "endpoint_key": target.endpoint.api_key,
            "model": target.name,
            "thinking": target.endpoint.options.thinking_level,
            "eval": self.config.eval_kwargs,
            "mcp_servers": [vars(server) for server in self.config.kwargs["mcp_servers"]],
        }
        with open(os.environ["FAKE_SDK_RECORD"], "w") as handle:
            json.dump(record, handle)
        return _Response(prompt)
""",
    "google/antigravity/hooks.py": """
class policy:
    @staticmethod
    def allow_all():
        return "allow_all"
""",
    "google/antigravity/models.py": """
class ThinkingLevel:
    MINIMAL = "MINIMAL"
    LOW = "LOW"
    MEDIUM = "MEDIUM"
    HIGH = "HIGH"
    EXTRA_HIGH = "EXTRA_HIGH"


class GeminiModelOptions:
    def __init__(self, thinking_level=None):
        self.thinking_level = thinking_level


class GeminiAPIEndpoint:
    def __init__(self, api_key, options):
        self.api_key = api_key
        self.options = options


class ModelTarget:
    def __init__(self, name, endpoint):
        self.name = name
        self.endpoint = endpoint
""",
    "google/antigravity/types.py": """
class McpStreamableHttpServer:
    def __init__(self, name, url):
        self.name = name
        self.url = url
""",
    "google_antigravity-0.1.20.dist-info/METADATA": "Metadata-Version: 2.1\nName: google-antigravity\nVersion: 0.1.20\n",
}


def _fake_sdk(root: Path) -> Path:
    """Write a fake ``google.antigravity`` package so the runner executes without the real SDK.

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


def _run_runner(tmp_path: Path, launch_files: dict[str, str], *args: str, **env: str) -> subprocess.CompletedProcess:
    """Run the generated runner with the current Python against the fake SDK.

    Args:
        tmp_path: Test directory; the workspace and fake SDK live under it.
        launch_files: Generated launch files to write into the workspace.
        *args: Runner arguments.
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
        [sys.executable, RUNNER_FILE, *args],
        cwd=workspace,
        env={"PATH": os.defpath, "PYTHONPATH": str(_fake_sdk(tmp_path / "fake")), **env},
        capture_output=True,
        text=True,
        timeout=30,
        check=False,
    )


def test_spec_is_registered_with_a_pinned_version_and_the_pi_parser() -> None:
    """The registry holds the harness with an exact SDK pin and an existing parser."""
    assert REGISTRY[HARNESS_ID] is SPEC
    assert SPEC.version == ANTIGRAVITY_SDK_VERSION == "0.1.20"
    assert SPEC.output_format == "pi"
    assert SPEC.launch("m", _GATEWAY).parse_output is parse_pi_output


def test_launch_routes_through_the_gateway_without_a_direct_key() -> None:
    """The env carries the gateway token and blanks every direct Gemini or Google credential."""
    launch = SPEC.launch("gemini-3-pro", _GATEWAY)

    assert launch.env["ASCENT_MODEL"] == "gemini-3-pro"
    assert launch.env["ASCENT_MODEL_URL"] == _GATEWAY.url
    assert launch.env["ASCENT_MODEL_KEY"] == _GATEWAY.api_key
    for name in _DIRECT_KEYS:
        assert launch.env[name] == ""
    assert "GOOGLE_GEMINI_BASE_URL" not in launch.env
    assert ENV_THINKING_LEVEL not in launch.env
    assert list(launch.files) == [RUNNER_FILE]
    assert launch.run_command == f'PATH="$HOME/.local/bin:$PATH" uv run --quiet --script {RUNNER_FILE}'
    assert launch.instructions_file == "AGENTS.md"


def test_runner_pins_exact_dependencies_and_embeds_the_gateway_url() -> None:
    """The uv script pins every dependency exactly and holds the gateway URL as a literal."""
    runner = SPEC.launch("m", _GATEWAY).files[RUNNER_FILE]

    compile(runner, RUNNER_FILE, "exec")
    assert runner.startswith("#!/usr/bin/env -S uv run --script\n")
    for pin in ('"google-antigravity==0.1.20"', '"cryptography==46.0.7"', '"fastapi==0.136.3"'):
        assert pin in runner
    assert 'requires-python = ">=3.12,<3.15"' in runner
    assert f"GATEWAY_URL = {_GATEWAY.url!r}" in runner
    assert _GATEWAY.api_key not in runner
    assert "Adapted from Harbor 0.25.0 (Apache-2.0)" in runner


def test_install_pins_uv_and_warms_the_sdk_environment() -> None:
    """The open-mode install reuses the repo's uv pin and checks the SDK version first."""
    install = SPEC.launch("m", _GATEWAY).install_command

    assert install is not None
    assert install.startswith('export PATH="$HOME/.local/bin:$PATH"; ')
    assert _version_check(SPEC.binary, ANTIGRAVITY_SDK_VERSION) in install
    assert "uv==0.9.13" in install
    assert "curl" not in install
    assert RUNNER_FILE not in install
    assert install.endswith("""print(m.version("google-antigravity"))' > /dev/null; }""")


def test_version_probe_needs_no_workspace_file_and_never_downloads() -> None:
    """The probe resolves the exact script pins offline, so it runs before the launch files exist."""
    assert RUNNER_FILE not in SPEC.binary
    assert SPEC.binary.startswith("uv run --quiet --offline --no-project ")
    for pin in ("google-antigravity==0.1.20", "cryptography==46.0.7", "fastapi==0.136.3"):
        assert f"--with {pin}" in SPEC.binary


@pytest.mark.parametrize(
    ("effort", "level"),
    [("minimal", "MINIMAL"), ("low", "LOW"), ("medium", "MEDIUM"), ("high", "HIGH"), ("xhigh", "EXTRA_HIGH")],
)
def test_supported_effort_maps_to_its_own_gemini_thinking_level(effort: str, level: str) -> None:
    """Each declared level maps to a distinct ``ThinkingLevel`` member of google-antigravity 0.1.20.

    Args:
        effort: The Skynet effort.
        level: The name of the ``ThinkingLevel`` member it must map to.
    """
    assert effort in SPEC.supported_efforts
    assert SPEC.launch("m", _GATEWAY, reasoning_effort=effort).env[ENV_THINKING_LEVEL] == level


def test_spec_declares_one_level_per_gemini_thinking_level() -> None:
    """The spec offers five levels, so no two Skynet levels share one Gemini level."""
    assert SPEC.supported_efforts == ("minimal", "low", "medium", "high", "xhigh")


@pytest.mark.parametrize("effort", ["none", "max", "HIGH", "turbo"])
def test_unsupported_effort_is_refused_at_launch(effort: str) -> None:
    """A level with no Gemini thinking level stops the launch instead of a silent remap or drop.

    Args:
        effort: An effort with no Gemini thinking level.
    """
    with pytest.raises(ServiceError, match="does not support the effort level"):
        SPEC.launch("m", _GATEWAY, reasoning_effort=effort)


@pytest.mark.parametrize("effort", [None, ""])
def test_no_effort_keeps_the_sdk_default(effort: str | None) -> None:
    """No effort sets no thinking level, so the SDK keeps its own default.

    Args:
        effort: An empty effort.
    """
    assert ENV_THINKING_LEVEL not in SPEC.launch("m", _GATEWAY, reasoning_effort=effort).env


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
    """No gateway URL means no launch; the harness never falls back to a direct key.

    Args:
        url: A gateway URL that is missing, relative or not clean.
    """
    with pytest.raises(ServiceError, match="Antigravity SDK"):
        SPEC.launch("m", GatewayConfig(url=url, api_key="secret-key"))


def test_runner_sends_the_turn_to_the_gateway_and_prints_a_pi_event(tmp_path: Path) -> None:
    """Against a fake SDK, the runner sets the gateway base URL, drops direct keys and prints parseable output.

    Args:
        tmp_path: Test directory; the workspace and fake SDK live under it.
    """
    launch = SPEC.launch("gemini-3-pro", _GATEWAY, reasoning_effort="high")
    prompt = tmp_path / "prompt.md"
    prompt.write_text("Fix the bug.\n")
    record = tmp_path / "record.json"

    result = _run_runner(
        tmp_path,
        launch.files,
        **{
            **launch.env,
            "ASCENT_TASK": str(prompt),
            "GEMINI_API_KEY": "leaked-direct-key",
            "GOOGLE_API_KEY": "leaked-direct-key",
            "FAKE_SDK_RECORD": str(record),
        },
    )

    assert result.returncode == 0, result.stderr
    seen = json.loads(record.read_text())
    assert seen == {
        "base_url": _GATEWAY.url,
        "direct_keys": {"GEMINI_API_KEY": None, "GOOGLE_API_KEY": None},
        "api_key": _GATEWAY.api_key,
        "endpoint_key": _GATEWAY.api_key,
        "model": "gemini-3-pro",
        "thinking": "HIGH",
        "eval": {"thinking_level": None},
        "mcp_servers": [],
    }
    assert parse_pi_output(result.stdout) == ("answer to Fix the bug.", {"input_tokens": 11, "output_tokens": 7})
    assert (tmp_path / "ws" / ".app/antigravity/session").is_dir()


def test_launch_without_proposer_mcp_servers_keeps_the_runner() -> None:
    """No proposer MCP servers leaves the runner exactly as before, with an empty server list."""
    runner = SPEC.launch("m", _GATEWAY, mcp_servers=()).files[RUNNER_FILE]
    assert runner == SPEC.launch("m", _GATEWAY).files[RUNNER_FILE]
    assert "        mcp_servers=[],\n" in runner
    assert "McpStreamableHttpServer" not in runner


def test_runner_refuses_without_the_gateway_token(tmp_path: Path) -> None:
    """The runner exits before any SDK call when the gateway token is missing.

    Args:
        tmp_path: Test directory; the workspace and fake SDK live under it.
    """
    launch = SPEC.launch("m", _GATEWAY)
    record = tmp_path / "record.json"

    result = _run_runner(
        tmp_path, launch.files, **{**launch.env, "ASCENT_MODEL_KEY": "", "FAKE_SDK_RECORD": str(record)}
    )

    assert result.returncode != 0
    assert "gateway" in result.stderr
    assert not record.exists()


# A fake uv that refuses the workspace script, needs the exact pins, and
# answers an offline run only from a warmed cache (the marker file).
_FAKE_UV = """#!/bin/sh
case " $* " in *" --script "*) echo "no such script" >&2; exit 2;; esac
case " $* " in *" --no-project "*) ;; *) exit 3;; esac
case " $* " in *" --with google-antigravity==0.1.20 "*) ;; *) exit 4;; esac
case " $* " in
  *" --offline "*) test -f "$FAKE_UV_CACHE" || exit 5;;
  *) : > "$FAKE_UV_CACHE";;
esac
echo 0.1.20
"""


def test_recorded_output_sample_parses_to_the_final_answer_and_usage() -> None:
    """The Pi parser reads the runner's recorded stream and ignores the non-JSON noise."""
    assert parse_pi_output(_RECORDED_STDOUT) == (
        "Patched parse_date and the tests pass.",
        {"input_tokens": 1834, "output_tokens": 421},
    )
