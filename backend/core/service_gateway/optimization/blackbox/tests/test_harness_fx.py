"""Tests for the fx harness: registry entry, gateway-only launch, protected env and output parsing."""

from __future__ import annotations

import json
import os
import stat
import subprocess
from pathlib import Path

import pytest

from core.models.blackbox import BLACKBOX_HARNESS_FX, BLACKBOX_HARNESSES, BLACKBOX_TARGET_AGENT, BlackboxTarget

from ..harness import GatewayConfig, build_launch, launch_payload, pinned_harness_check
from ..harness_bridge import parse_output, parse_plain_output
from ..harnesses import REGISTRY
from ..harnesses.fx import FX_SETTINGS_FILE, FX_VERSION

_GATEWAY = GatewayConfig(url="https://gw.example/v1", api_key="gateway-token")
_RELAY = "http://127.0.0.1:3210/v1"
_DIRECT_KEYS = ("AI_GATEWAY_API_KEY", "VERCEL_AI_GATEWAY_API_KEY", "VERCEL_OIDC_TOKEN", "OPENAI_API_KEY")
# Synthetic one-shot transcript in the shape of ``fx ask --yolo`` plain output.
_RECORDED_STDOUT = "\nI read solution.py and fixed the off-by-one error.\n\nThe answer is 42.\n\n"
# Stand-in for the fx binary: it records its call and the settings it reads, then prints a fixed answer.
_FAKE_FX = """#!/bin/sh
{
  printf 'ARGS'; for a in "$@"; do printf ' [%s]' "$a"; done; printf '\\n'
  printf 'FX_MODEL=%s\\n' "$FX_MODEL"
  printf 'FX_PROVIDER=%s\\n' "$FX_PROVIDER"
  printf 'APP_API_KEY=%s\\n' "$APP_API_KEY"
  printf 'AI_GATEWAY_API_KEY=%s\\n' "$AI_GATEWAY_API_KEY"
  printf 'SETTINGS='; cat "$HOME/.fx/settings.json" | tr -d '\\n'; printf '\\n'
} >> "$FAKE_LOG"
printf '%s' "$FAKE_ANSWER"
"""


def _target() -> BlackboxTarget:
    """Build an fx agent target.

    Returns:
        The target.
    """
    return BlackboxTarget(kind=BLACKBOX_TARGET_AGENT, harness=BLACKBOX_HARNESS_FX, model="target-model")


def _settings(launch_files: dict[str, str]) -> dict[str, object]:
    """Read the generated fx settings from a launch's files.

    Args:
        launch_files: The launch's generated files.

    Returns:
        The parsed settings.
    """
    return json.loads(launch_files[FX_SETTINGS_FILE])


def _run_with_fake_fx(tmp_path: Path, *, protected: bool) -> tuple[str, str]:
    """Run the built fx command in a scratch workspace against a fake ``fx`` binary.

    Args:
        tmp_path: Scratch directory for the workspace, the fake binary and its log.
        protected: Build the protected launch, with the sandbox-local relay set.

    Returns:
        The command's stdout and the fake binary's call log.
    """
    launch = build_launch(_target(), _GATEWAY, protected=protected)
    workspace = tmp_path / "work"
    for name, content in launch.files.items():
        path = workspace / name
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(content)
    (workspace / "task").mkdir(parents=True)
    (workspace / "task" / "PROMPT.md").write_text("Fix `solution.py` and say $(echo done).")
    bin_dir = tmp_path / "home" / ".local" / "bin"
    bin_dir.mkdir(parents=True)
    fake = bin_dir / "fx"
    fake.write_text(_FAKE_FX)
    fake.chmod(fake.stat().st_mode | stat.S_IEXEC)
    log = tmp_path / "calls.log"
    env = {
        **launch.env,
        "PATH": os.defpath,
        "HOME": str(tmp_path / "home"),
        "FAKE_LOG": str(log),
        "FAKE_ANSWER": _RECORDED_STDOUT,
    }
    if protected:
        env["APP_BUDGET_RELAY_URL"] = _RELAY
    result = subprocess.run(
        launch.run_command, shell=True, cwd=workspace, env=env, capture_output=True, text=True, timeout=10, check=True
    )
    return result.stdout, log.read_text()


def test_fx_is_registered_before_cline_with_its_pin() -> None:
    """List fx in the registry and the request model, pinned to the exact release."""
    spec = REGISTRY[BLACKBOX_HARNESS_FX]
    assert (spec.binary, spec.version, spec.output_format, spec.position) == ("fx", "0.0.16", "plain", 62)
    assert list(REGISTRY)[-2:] == [BLACKBOX_HARNESS_FX, "cline"]
    assert BLACKBOX_HARNESS_FX in BLACKBOX_HARNESSES
    assert pinned_harness_check(BLACKBOX_HARNESS_FX) == "fx --version 2>/dev/null | grep -Eq '(^| )0[.]0[.]16( |$)'"


def test_open_launch_installs_the_pin_and_sends_model_calls_only_to_the_gateway() -> None:
    """Install the pinned release, seed the gateway provider from a file, and keep direct keys out of the env."""
    launch = build_launch(_target(), _GATEWAY)

    assert launch.instructions_file == "AGENTS.md"
    assert launch.install_command == (
        'export PATH="$HOME/.local/bin:$PATH"; '
        "fx --version 2>/dev/null | grep -Eq '(^| )0[.]0[.]16( |$)' "
        f"|| curl -fsSL https://fx.sh/setup.sh | bash -s -- v{FX_VERSION} >/dev/null && fx --version"
    )
    assert _settings(launch.files) == {
        "providers": {
            "runtime": {
                "protocol": "openai-chat-completions",
                "base_url": _GATEWAY.url,
                "auth": {"type": "bearer", "env": "APP_API_KEY"},
            }
        }
    }
    assert _GATEWAY.url not in launch.run_command
    assert _GATEWAY.api_key not in launch.run_command
    assert _GATEWAY.api_key not in launch.files[FX_SETTINGS_FILE]
    assert launch.run_command == (
        'export PATH="$HOME/.local/bin:$PATH"; mkdir -p "$HOME/.fx" && '
        f'cp {FX_SETTINGS_FILE} "$HOME/.fx/settings.json" && '
        'fx ask --yolo -- "$(cat "$APP_PROMPT_FILE")" < /dev/null'
    )
    assert {name: launch.env[name] for name in ("FX_MODEL", "FX_PROVIDER", "FX_AUTO_UPGRADE", "FX_SOUND")} == {
        "FX_MODEL": "target-model",
        "FX_PROVIDER": "runtime",
        "FX_AUTO_UPGRADE": "0",
        "FX_SOUND": "0",
    }
    assert launch.env["APP_API_KEY"] == _GATEWAY.api_key
    assert launch.env["APP_GATEWAY_URL"] == _GATEWAY.url
    assert not set(_DIRECT_KEYS) & set(launch.env)
    assert launch.parse_output is parse_plain_output


@pytest.mark.parametrize(
    ("effort", "expected"),
    [(None, None), ("none", "none"), ("minimal", "minimal"), ("high", "high"), ("max", "max"), ("turbo", None)],
)
def test_reasoning_effort_goes_into_the_settings(effort: str | None, expected: str | None) -> None:
    """Write the user's effort as fx's ``effort`` setting, or leave fx's default for an unknown name."""
    settings = _settings(build_launch(_target(), _GATEWAY, reasoning_effort=effort).files)
    assert settings.get("effort") == expected


def test_protected_launch_checks_the_pin_offline_and_blocks_direct_routes() -> None:
    """Use the offline version probe, blank every direct Vercel route, and keep the gateway key."""
    launch = build_launch(_target(), _GATEWAY, protected=True)

    assert launch.install_command == pinned_harness_check(BLACKBOX_HARNESS_FX)
    for name in (
        "AI_GATEWAY_API_KEY",
        "VERCEL_AI_GATEWAY_API_KEY",
        "VERCEL_OIDC_TOKEN",
        "FX_GATEWAY_BASE_URL",
        "FX_GATEWAY_CHAT_URL",
        "FX_E2E_GATEWAY_CHAT_URL",
    ):
        assert launch.env[name] == ""
    assert launch.env["DO_NOT_TRACK"] == "1"
    assert launch.env["FX_AUTO_UPGRADE"] == "0"
    assert launch.env["APP_API_KEY"] == _GATEWAY.api_key
    assert "OPENAI_API_KEY" not in launch.env


def test_protected_run_points_fx_at_the_relay_and_returns_its_answer(tmp_path: Path) -> None:
    """Run the protected command against a fake fx: the relay URL, gateway token and answer come through."""
    stdout, calls = _run_with_fake_fx(tmp_path, protected=True)

    run = next(line for line in calls.splitlines() if line.startswith("ARGS"))
    assert run == "ARGS [ask] [--yolo] [--] [Fix `solution.py` and say $(echo done).]"
    assert "FX_MODEL=target-model\n" in calls
    assert "FX_PROVIDER=runtime\n" in calls
    assert "APP_API_KEY=gateway-token\n" in calls
    assert "AI_GATEWAY_API_KEY=\n" in calls
    settings = json.loads(next(line for line in calls.splitlines() if line.startswith("SETTINGS=")).split("=", 1)[1])
    assert settings["providers"]["runtime"]["base_url"] == _RELAY
    assert _GATEWAY.url not in calls
    assert parse_output("plain", stdout) == (
        "I read solution.py and fixed the off-by-one error.\n\nThe answer is 42.",
        {},
    )


def test_open_run_reads_the_gateway_url_from_the_generated_file(tmp_path: Path) -> None:
    """Without a relay, give fx the gateway URL the launch wrote."""
    _, calls = _run_with_fake_fx(tmp_path, protected=False)

    settings = json.loads(next(line for line in calls.splitlines() if line.startswith("SETTINGS=")).split("=", 1)[1])
    assert settings["providers"]["runtime"]["base_url"] == _GATEWAY.url


def test_recorded_output_parses_to_the_answer_and_the_payload_names_the_plain_parser() -> None:
    """Parse a recorded transcript to its stripped text, and serialize the launch with the bridge's plain parser."""
    assert parse_plain_output(_RECORDED_STDOUT) == (
        "I read solution.py and fixed the off-by-one error.\n\nThe answer is 42.",
        {},
    )
    assert parse_plain_output("  \n") == (None, {})
    assert launch_payload(build_launch(_target(), _GATEWAY))["output_format"] == "plain"
