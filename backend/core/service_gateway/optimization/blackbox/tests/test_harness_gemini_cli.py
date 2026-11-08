"""Tests for the Gemini CLI harness: registry entry, gateway-only launch, protected env and output parsing."""

from __future__ import annotations

import json
import os
import shlex
import shutil
import stat
import subprocess
from pathlib import Path

import pytest

from core.models.blackbox import BLACKBOX_HARNESS_GEMINI_CLI, BLACKBOX_HARNESSES, BLACKBOX_TARGET_AGENT, BlackboxTarget

from ..harness import GatewayConfig, build_launch, launch_payload, pinned_harness_check
from ..harness_bridge import parse_output, parse_plain_output
from ..harnesses import REGISTRY
from ..harnesses.gemini_cli import GEMINI_BASE_URL_FILE, GEMINI_CLI_VERSION

_GATEWAY = GatewayConfig(url="https://gw.example/v1", api_key="gateway-token")
_RELAY = "http://127.0.0.1:3210/v1"
_PROVIDER_KEYS = ("GEMINI_API_KEY", "GOOGLE_API_KEY", "GOOGLE_APPLICATION_CREDENTIALS", "OPENAI_API_KEY")
# Synthetic one-shot transcript in the shape of ``gemini --yolo --prompt`` plain output.
_RECORDED_STDOUT = "\nI read solution.py and fixed the off-by-one error.\n\nThe answer is 42.\n\n"
# Stand-in for the Gemini binary: it records its call, env and settings, and prints a fixed answer.
_FAKE_GEMINI = """#!/bin/sh
{
  printf 'ARGS'; for a in "$@"; do printf ' [%s]' "$a"; done; printf '\\n'
  printf 'GEMINI_API_KEY=%s\\n' "$GEMINI_API_KEY"
  printf 'GOOGLE_API_KEY=%s\\n' "$GOOGLE_API_KEY"
  printf 'GOOGLE_GEMINI_BASE_URL=%s\\n' "$GOOGLE_GEMINI_BASE_URL"
  printf 'GEMINI_CLI_HOME=%s\\n' "$GEMINI_CLI_HOME"
  printf 'GEMINI_CLI_TRUST_WORKSPACE=%s\\n' "$GEMINI_CLI_TRUST_WORKSPACE"
  printf 'GEMINI_TELEMETRY_ENABLED=%s\\n' "$GEMINI_TELEMETRY_ENABLED"
} >> "$FAKE_LOG"
printf '%s' "$FAKE_ANSWER"
"""


def _target() -> BlackboxTarget:
    """Build a Gemini CLI agent target.

    Returns:
        The target.
    """
    return BlackboxTarget(kind=BLACKBOX_TARGET_AGENT, harness=BLACKBOX_HARNESS_GEMINI_CLI, model="target-model")


def _settings(run_command: str) -> dict[str, object]:
    """Return the ``settings.json`` the run command writes into Gemini's home.

    Args:
        run_command: The launch's shell command.

    Returns:
        The parsed settings.
    """
    words = shlex.split(run_command)
    return json.loads(words[words.index("printf") + 2])


def _log_value(calls: str, name: str) -> str:
    """Return one ``NAME=value`` line's value from the fake binary's log.

    Args:
        calls: The fake binary's call log.
        name: The variable name.

    Returns:
        The logged value.
    """
    return next(line for line in calls.splitlines() if line.startswith(f"{name}=")).split("=", 1)[1]


def _run_with_fake_gemini(tmp_path: Path, *, protected: bool) -> tuple[str, str, Path]:
    """Run the built Gemini command in a scratch workspace against a fake ``gemini`` binary.

    An inherited ``GEMINI_API_KEY`` is set on purpose: the run must replace it with the gateway token.

    Args:
        tmp_path: Scratch directory for the workspace, the fake binary and its log.
        protected: Build the protected launch, with the sandbox-local relay set.

    Returns:
        The command's stdout, the fake binary's call log, and the workspace.
    """
    launch = build_launch(_target(), _GATEWAY, protected=protected)
    workspace = tmp_path / "work"
    for name, content in launch.files.items():
        path = workspace / name
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(content)
    (workspace / "task").mkdir(parents=True)
    (workspace / "task" / "PROMPT.md").write_text("Fix `solution.py` and say $(echo done).")
    bin_dir = tmp_path / "bin"
    bin_dir.mkdir()
    fake = bin_dir / "gemini"
    fake.write_text(_FAKE_GEMINI)
    fake.chmod(fake.stat().st_mode | stat.S_IEXEC)
    log = tmp_path / "calls.log"
    env = {
        **launch.env,
        "PATH": f"{bin_dir}{os.pathsep}{os.defpath}",
        "HOME": str(tmp_path / "home"),
        "GEMINI_API_KEY": "inherited-real-key",
        "FAKE_LOG": str(log),
        "FAKE_ANSWER": _RECORDED_STDOUT,
    }
    if protected:
        env["APP_BUDGET_RELAY_URL"] = _RELAY
    result = subprocess.run(
        launch.run_command, shell=True, cwd=workspace, env=env, capture_output=True, text=True, timeout=10, check=True
    )
    return result.stdout, log.read_text(), workspace


def test_gemini_cli_is_registered_before_cline_with_its_pin() -> None:
    """List Gemini CLI in the registry and the request model, pinned to the exact npm release."""
    spec = REGISTRY[BLACKBOX_HARNESS_GEMINI_CLI]
    assert (spec.binary, spec.version, spec.output_format, spec.position) == ("gemini", "0.67.0", "plain", 63)
    names = list(REGISTRY)
    assert names.index(BLACKBOX_HARNESS_GEMINI_CLI) < names.index("cline")
    assert BLACKBOX_HARNESS_GEMINI_CLI in BLACKBOX_HARNESSES
    assert (
        pinned_harness_check(BLACKBOX_HARNESS_GEMINI_CLI)
        == "gemini --version 2>/dev/null | grep -Eq '(^| )0[.]67[.]0( |$)'"
    )


def test_open_launch_installs_the_pin_and_sends_model_calls_only_to_the_gateway() -> None:
    """Install the pinned package, read the gateway URL from a file, and keep provider keys out of the env."""
    launch = build_launch(_target(), _GATEWAY)

    assert launch.instructions_file == "GEMINI.md"
    assert launch.install_command is not None
    assert launch.install_command.endswith(f"|| npm install -g @google/gemini-cli@{GEMINI_CLI_VERSION}")
    assert launch.files == {GEMINI_BASE_URL_FILE: _GATEWAY.url}
    assert _GATEWAY.url not in launch.run_command
    assert _GATEWAY.api_key not in launch.run_command
    assert f'GOOGLE_GEMINI_BASE_URL="$(cat {GEMINI_BASE_URL_FILE})"' in launch.run_command
    assert 'GEMINI_API_KEY="$APP_API_KEY" ' in launch.run_command
    assert 'gemini --yolo --model="$APP_MODEL" --prompt="$(cat "$APP_PROMPT_FILE")" < /dev/null' in launch.run_command
    assert launch.env["APP_API_KEY"] == _GATEWAY.api_key
    assert launch.env["APP_GATEWAY_URL"] == _GATEWAY.url
    assert not set(_PROVIDER_KEYS) & set(launch.env)
    assert launch.parse_output is parse_plain_output


def test_settings_force_api_key_auth_and_turn_off_auto_update_and_telemetry() -> None:
    """Write settings that pick API-key auth and disable auto-update, usage statistics and telemetry."""
    settings = _settings(build_launch(_target(), _GATEWAY).run_command)

    assert settings == {
        "security": {"auth": {"selectedType": "gemini-api-key"}},
        "general": {"enableAutoUpdate": False, "enableAutoUpdateNotification": False},
        "privacy": {"usageStatisticsEnabled": False},
        "telemetry": {"enabled": False},
    }


@pytest.mark.parametrize(
    ("effort", "level"),
    [
        (None, None),
        ("none", None),
        ("minimal", "MINIMAL"),
        ("low", "LOW"),
        ("medium", "MEDIUM"),
        ("high", "HIGH"),
        ("xhigh", "HIGH"),
        ("max", "HIGH"),
    ],
)
def test_reasoning_effort_maps_to_a_thinking_level_alias(effort: str | None, level: str | None) -> None:
    """Carry the user's effort as a settings alias with Gemini's thinking level, or keep the plain model."""
    launch = build_launch(_target(), _GATEWAY, reasoning_effort=effort)
    settings = _settings(launch.run_command)
    if level is None:
        assert "modelConfigs" not in settings
        assert '--model="$APP_MODEL"' in launch.run_command
        return
    alias = f"skynet-{level.lower()}"
    assert f"--model={alias} " in launch.run_command
    assert settings["modelConfigs"]["customAliases"] == {
        alias: {
            "modelConfig": {
                "model": "target-model",
                "generateContentConfig": {"thinkingConfig": {"includeThoughts": True, "thinkingLevel": level}},
            }
        }
    }


def test_protected_launch_checks_the_pin_offline_and_blocks_other_routes() -> None:
    """Use the offline version probe, blank Google credentials, turn off Vertex, Code Assist and telemetry."""
    launch = build_launch(_target(), _GATEWAY, protected=True)

    assert launch.install_command == pinned_harness_check(BLACKBOX_HARNESS_GEMINI_CLI)
    for name in ("GOOGLE_API_KEY", "GOOGLE_APPLICATION_CREDENTIALS", "GOOGLE_CLOUD_PROJECT"):
        assert launch.env[name] == ""
    assert launch.env["GOOGLE_GENAI_USE_VERTEXAI"] == "false"
    assert launch.env["GOOGLE_GENAI_USE_GCA"] == "false"
    assert launch.env["GEMINI_TELEMETRY_ENABLED"] == "false"
    assert launch.env["DO_NOT_TRACK"] == "1"
    assert launch.env["APP_API_KEY"] == _GATEWAY.api_key
    assert "GEMINI_API_KEY" not in launch.env


def test_protected_run_points_gemini_at_the_relay_and_returns_its_answer(tmp_path: Path) -> None:
    """Run the protected command against a fake Gemini: the relay URL, gateway token and answer come through."""
    stdout, calls, workspace = _run_with_fake_gemini(tmp_path, protected=True)

    (run,) = [line for line in calls.splitlines() if line.startswith("ARGS")]
    assert run == "ARGS [--yolo] [--model=target-model] [--prompt=Fix `solution.py` and say $(echo done).]"
    assert _log_value(calls, "GOOGLE_GEMINI_BASE_URL") == _RELAY
    assert _GATEWAY.url not in calls
    assert _log_value(calls, "GEMINI_API_KEY") == "gateway-token"
    assert "inherited-real-key" not in calls
    assert _log_value(calls, "GOOGLE_API_KEY") == ""
    assert _log_value(calls, "GEMINI_TELEMETRY_ENABLED") == "false"
    assert _log_value(calls, "GEMINI_CLI_TRUST_WORKSPACE") == "true"
    home = Path(_log_value(calls, "GEMINI_CLI_HOME"))
    assert not home.is_relative_to(workspace.resolve())
    assert not home.is_relative_to(workspace)
    settings = json.loads((home / ".gemini" / "settings.json").read_text())
    shutil.rmtree(home)
    assert settings == _settings(build_launch(_target(), _GATEWAY, protected=True).run_command)
    assert settings["security"]["auth"]["selectedType"] == "gemini-api-key"
    assert not list(workspace.rglob("settings.json"))
    assert parse_output("plain", stdout) == (
        "I read solution.py and fixed the off-by-one error.\n\nThe answer is 42.",
        {},
    )


def test_open_run_reads_the_gateway_url_from_the_generated_file(tmp_path: Path) -> None:
    """Without a relay, point Gemini at the gateway URL the launch wrote."""
    _, calls, _ = _run_with_fake_gemini(tmp_path, protected=False)

    assert _log_value(calls, "GOOGLE_GEMINI_BASE_URL") == _GATEWAY.url
    assert _log_value(calls, "GEMINI_API_KEY") == "gateway-token"
    shutil.rmtree(_log_value(calls, "GEMINI_CLI_HOME"))


def test_recorded_output_parses_to_the_answer_and_the_payload_names_the_plain_parser() -> None:
    """Parse a recorded transcript to its stripped text, and serialize the launch with the bridge's plain parser."""
    assert parse_plain_output(_RECORDED_STDOUT) == (
        "I read solution.py and fixed the off-by-one error.\n\nThe answer is 42.",
        {},
    )
    assert parse_plain_output("  \n") == (None, {})
    assert launch_payload(build_launch(_target(), _GATEWAY))["output_format"] == "plain"
