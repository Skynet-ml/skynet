"""Tests for the eve harness: registration, the generated project, gateway-only env and the runner output."""

from __future__ import annotations

import json
import os
import subprocess
from pathlib import Path

from core.models.blackbox import BLACKBOX_HARNESS_EVE, BLACKBOX_HARNESSES, BLACKBOX_TARGET_AGENT, BlackboxTarget

from ..harness import GatewayConfig, build_launch, launch_payload, pinned_harness_check
from ..harness_bridge import parse_output, parse_pi_output, tool_call_ids
from ..harnesses import REGISTRY
from ..harnesses.eve import (
    AI_SDK_VERSION,
    EVE_INSTRUCTIONS_FILE,
    EVE_RUNNER,
    EVE_VERSION,
    OPENAI_COMPATIBLE_VERSION,
    ZOD_VERSION,
)

_GATEWAY = GatewayConfig(url="https://gw.example/v1", api_key="secret-key")
_PROVIDER_KEYS = (
    "AI_GATEWAY_API_KEY",
    "ANTHROPIC_API_KEY",
    "AWS_ACCESS_KEY_ID",
    "AWS_SECRET_ACCESS_KEY",
    "OPENAI_API_KEY",
    "OPENAI_BASE_URL",
    "OPENROUTER_API_KEY",
    "VERCEL_OIDC_TOKEN",
)
# A synthetic runner transcript in the shape runner.mjs prints: one line per
# requested eve action, then the final answer with the summed step usage.
_RUNNER_SAMPLE = "\n".join(
    [
        "eve build complete",
        json.dumps({"type": "tool_execution_start", "toolCallId": "act_1"}),
        json.dumps({"type": "tool_execution_start", "toolCallId": "act_2"}),
        json.dumps(
            {
                "type": "message_end",
                "message": {
                    "role": "assistant",
                    "content": [{"type": "text", "text": "The answer is 42."}],
                    "usage": {"input": 1200, "output": 85},
                },
            }
        ),
    ]
)


def _target(**overrides: str) -> BlackboxTarget:
    """Build an eve agent target.

    Args:
        overrides: Extra target fields.

    Returns:
        The target.
    """
    return BlackboxTarget(kind=BLACKBOX_TARGET_AGENT, model="openrouter/acme/agent-1", harness="eve", **overrides)


def test_eve_is_registered_after_prime_and_before_cline() -> None:
    """List eve in the registry and the model ids between prime and cline."""
    spec = REGISTRY[BLACKBOX_HARNESS_EVE]
    assert (spec.binary, spec.version, spec.output_format, spec.position) == ("eve", "1.0.0-rc.6", "pi", 60)
    assert BLACKBOX_HARNESSES[-4:] == ("prime", "eve", "cline", "custom")
    assert list(REGISTRY)[-2:] == ["eve", "cline"]


def _write_fixed_files(run_command: str, root: Path) -> None:
    """Run only the part of the eve run command that writes the fixed project files.

    Args:
        run_command: The launch run command.
        root: The fake workspace.
    """
    (root / ".app/eve/agent").mkdir(parents=True)
    write_fixed = run_command.split(" && (cd .app/eve", 1)[0]
    subprocess.run(write_fixed, shell=True, cwd=root, env={"PATH": os.environ["PATH"]}, timeout=10, check=True)


def test_launch_generates_a_pinned_eve_project(tmp_path: Path) -> None:
    """Write package.json with exact pins, an agent on the gateway, and the runner."""
    launch = build_launch(_target(), _GATEWAY)
    assert launch.instructions_file == EVE_INSTRUCTIONS_FILE == ".app/eve/agent/instructions.md"
    assert set(launch.files) == {".app/eve/agent/agent.ts"}
    agent = launch.files[".app/eve/agent/agent.ts"]
    assert 'baseURL: "https://gw.example/v1"' in agent
    assert "apiKey: process.env.APP_API_KEY" in agent
    assert 'gateway("acme/agent-1")' in agent
    assert "secret-key" not in agent
    _write_fixed_files(launch.run_command, tmp_path)
    package = json.loads((tmp_path / ".app/eve/package.json").read_text())
    assert package["dependencies"] == {
        "@ai-sdk/openai-compatible": OPENAI_COMPATIBLE_VERSION,
        "ai": AI_SDK_VERSION,
        "eve": EVE_VERSION,
        "zod": ZOD_VERSION,
    }
    assert all(version[0].isdigit() for version in package["dependencies"].values())
    assert (tmp_path / ".app/eve/runner.mjs").read_text() == EVE_RUNNER


def test_launch_installs_the_pinned_cli_and_runs_the_runner() -> None:
    """Install eve at the pin, build the project after the box files land, then run the runner."""
    launch = build_launch(_target(), _GATEWAY)
    assert launch.install_command == (
        "eve --version 2>/dev/null | grep -Eq '(^| )1[.]0[.]0-rc[.]6( |$)' || npm install -g eve@1.0.0-rc.6"
    )
    assert launch.run_command.startswith("printf '%s' ")
    assert " && (cd .app/eve && npm install " in launch.run_command
    assert "node_modules/.bin/eve build) >&2 && " in launch.run_command
    assert launch.run_command.endswith('EVE_PROJECT_DIR="$PWD/.app/eve" node .app/eve/runner.mjs')
    assert launch_payload(launch)["output_format"] == "pi"


def test_env_carries_only_the_gateway_and_turns_telemetry_off() -> None:
    """Give eve the gateway URL and key, no provider key, and telemetry off."""
    env = build_launch(_target(), _GATEWAY).env
    assert env["APP_GATEWAY_URL"] == "https://gw.example/v1"
    assert env["APP_API_KEY"] == "secret-key"
    assert env["APP_MODEL"] == "acme/agent-1"
    assert env["EVE_TELEMETRY_DISABLED"] == "1"
    assert env["NPM_CONFIG_UPDATE_NOTIFIER"] == "false"
    assert not set(_PROVIDER_KEYS) & set(env)


def test_runner_drops_direct_provider_keys_from_the_eve_server() -> None:
    """Delete every direct provider credential before the runner starts ``eve start``."""
    for key in _PROVIDER_KEYS:
        assert f'"{key}"' in EVE_RUNNER
    assert "for (const key of PROVIDER_ENV_KEYS) delete env[key];" in EVE_RUNNER
    assert '"start", "--host", host, "--port"' in EVE_RUNNER
    assert 'stdio: ["ignore", "pipe", "inherit"]' in EVE_RUNNER


def test_protected_launch_checks_the_pin_relays_the_gateway_and_blocks_updates(tmp_path: Path) -> None:
    """Probe the pinned CLI, rewrite the agent's gateway URL to the relay, and add the protected env."""
    launch = build_launch(_target(), _GATEWAY, protected=True)
    assert launch.install_command == pinned_harness_check("eve")
    assert "npm install -g" not in launch.install_command
    agent = tmp_path / ".app/eve/agent/agent.ts"
    agent.parent.mkdir(parents=True)
    agent.write_text(launch.files[".app/eve/agent/agent.ts"])
    relay = launch.run_command.split(" && printf '%s' ", 1)[0]
    subprocess.run(
        relay,
        shell=True,
        cwd=tmp_path,
        env={"PATH": os.environ["PATH"], "APP_BUDGET_RELAY_URL": "http://127.0.0.1:3210/v1"},
        timeout=10,
        check=True,
    )
    assert 'baseURL: "http://127.0.0.1:3210/v1"' in agent.read_text()
    assert launch.env["NPM_CONFIG_OFFLINE"] == "true"
    assert launch.env["EVE_TELEMETRY_DISABLED"] == "1"
    assert not set(_PROVIDER_KEYS) & set(launch.env)


def test_pi_parser_reads_the_recorded_runner_sample() -> None:
    """Read the answer and usage from the runner's Pi-shaped lines and skip plain log lines."""
    expected = ("The answer is 42.", {"input_tokens": 1200, "output_tokens": 85})
    assert parse_pi_output(_RUNNER_SAMPLE) == expected
    assert parse_output("pi", _RUNNER_SAMPLE) == expected


def test_tool_counter_counts_each_requested_eve_action() -> None:
    """Count one tool call per runner ``tool_execution_start`` line."""
    ids = [
        call_id
        for line in _RUNNER_SAMPLE.splitlines()
        if line.startswith("{")
        for call_id in tool_call_ids("pi", json.loads(line))
    ]
    assert ids == ["act_1", "act_2"]
