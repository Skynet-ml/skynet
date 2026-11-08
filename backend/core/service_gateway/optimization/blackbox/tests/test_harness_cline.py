"""Tests for the Cline CLI harness: registry entry, gateway-only launch, protected env and output parsing."""

from __future__ import annotations

import json
import os
import shutil
import stat
import subprocess
from pathlib import Path

import pytest

from core.models.blackbox import BLACKBOX_HARNESS_CLINE, BLACKBOX_HARNESSES, BLACKBOX_TARGET_AGENT, BlackboxTarget

from ..harness import GatewayConfig, build_launch, launch_payload, pinned_harness_check
from ..harness_bridge import parse_output, parse_plain_output
from ..harnesses import REGISTRY
from ..harnesses.cline import CLINE_BASE_URL_FILE, CLINE_VERSION

_GATEWAY = GatewayConfig(url="https://gw.example/v1", api_key="gateway-token")
_RELAY = "http://127.0.0.1:3210/v1"
_PROVIDER_KEYS = ("OPENAI_API_KEY", "ANTHROPIC_API_KEY", "CLINE_API_KEY", "OPENROUTER_API_KEY", "AI_GATEWAY_API_KEY")
# Synthetic one-shot transcript in the shape of ``cline --yolo`` plain output.
_RECORDED_STDOUT = "\nI read solution.py and fixed the off-by-one error.\n\nThe answer is 42.\n\n"
# Stand-in for the Cline binary: it records each call and prints a fixed answer.
_FAKE_CLINE = """#!/bin/sh
log="$FAKE_LOG"
{
  printf 'ARGS'; for a in "$@"; do printf ' [%s]' "$a"; done; printf '\\n'
  printf 'OPENAI_API_KEY=%s\\n' "$OPENAI_API_KEY"
  printf 'ANTHROPIC_API_KEY=%s\\n' "$ANTHROPIC_API_KEY"
  printf 'CLINE_DATA_DIR=%s\\n' "$CLINE_DATA_DIR"
} >> "$log"
if [ "$1" = "auth" ]; then echo "saved"; exit 0; fi
printf '%s' "$FAKE_ANSWER"
"""


def _target(**overrides: object) -> BlackboxTarget:
    """Build a Cline agent target with the given field overrides.

    Args:
        **overrides: Extra target fields.

    Returns:
        The target.
    """
    return BlackboxTarget(kind=BLACKBOX_TARGET_AGENT, harness=BLACKBOX_HARNESS_CLINE, model="target-model", **overrides)


def _run_with_fake_cline(tmp_path: Path, *, protected: bool) -> tuple[str, str, Path]:
    """Run the built Cline command in a scratch workspace against a fake ``cline`` binary.

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
    fake = bin_dir / "cline"
    fake.write_text(_FAKE_CLINE)
    fake.chmod(fake.stat().st_mode | stat.S_IEXEC)
    log = tmp_path / "calls.log"
    env = {
        **launch.env,
        "PATH": f"{bin_dir}{os.pathsep}{os.defpath}",
        "HOME": str(tmp_path / "home"),
        "FAKE_LOG": str(log),
        "FAKE_ANSWER": _RECORDED_STDOUT,
    }
    if protected:
        env["APP_BUDGET_RELAY_URL"] = _RELAY
    result = subprocess.run(
        launch.run_command, shell=True, cwd=workspace, env=env, capture_output=True, text=True, timeout=10, check=True
    )
    return result.stdout, log.read_text(), workspace


def test_cline_is_registered_after_the_other_built_ins_with_its_pin() -> None:
    """List Cline in the registry and the request model, pinned to the exact npm release."""
    spec = REGISTRY[BLACKBOX_HARNESS_CLINE]
    assert (spec.binary, spec.version, spec.output_format) == ("cline", "3.0.74", "plain")
    assert list(REGISTRY)[-1] == BLACKBOX_HARNESS_CLINE
    assert BLACKBOX_HARNESS_CLINE in BLACKBOX_HARNESSES
    assert (
        pinned_harness_check(BLACKBOX_HARNESS_CLINE) == "cline --version 2>/dev/null | grep -Eq '(^| )3[.]0[.]74( |$)'"
    )


def test_open_launch_installs_the_pin_and_sends_model_calls_only_to_the_gateway() -> None:
    """Install the pinned package, seed the gateway provider from a file, and keep provider keys out of the env."""
    launch = build_launch(_target(), _GATEWAY)

    assert launch.instructions_file == "AGENTS.md"
    assert launch.install_command is not None
    assert launch.install_command.endswith(f"|| npm install -g cline@{CLINE_VERSION}")
    assert launch.files == {CLINE_BASE_URL_FILE: _GATEWAY.url}
    assert _GATEWAY.url not in launch.run_command
    assert _GATEWAY.api_key not in launch.run_command
    assert f'--baseurl "$(cat {CLINE_BASE_URL_FILE})"' in launch.run_command
    assert 'cline auth --provider openai --apikey "$APP_API_KEY" --modelid "$APP_MODEL"' in launch.run_command
    assert 'cline -P openai -k "$APP_API_KEY" -m "$APP_MODEL" --yolo -- "$(cat "$APP_PROMPT_FILE")" < /dev/null' in (
        launch.run_command
    )
    assert launch.env["APP_API_KEY"] == _GATEWAY.api_key
    assert launch.env["APP_GATEWAY_URL"] == _GATEWAY.url
    assert not set(_PROVIDER_KEYS) & set(launch.env)
    assert launch.parse_output is parse_plain_output


@pytest.mark.parametrize(
    ("effort", "flag"),
    [(None, None), ("none", "none"), ("high", "high"), ("xhigh", "xhigh"), ("max", "xhigh"), ("minimal", "low")],
)
def test_reasoning_effort_maps_to_the_thinking_flag(effort: str | None, flag: str | None) -> None:
    """Pass the user's effort as Cline's ``--thinking`` level, or no flag for Cline's default."""
    launch = build_launch(_target(), _GATEWAY, reasoning_effort=effort)
    if flag is None:
        assert "--thinking" not in launch.run_command
    else:
        assert f"--yolo --thinking {flag} -- " in launch.run_command


def test_protected_launch_checks_the_pin_offline_and_blocks_other_providers() -> None:
    """Use the offline version probe, blank every other provider key, and keep the gateway key."""
    launch = build_launch(_target(), _GATEWAY, protected=True)

    assert launch.install_command == pinned_harness_check(BLACKBOX_HARNESS_CLINE)
    for name in ("ANTHROPIC_API_KEY", "CLINE_API_KEY", "OPENROUTER_API_KEY", "AI_GATEWAY_API_KEY", "V0_API_KEY"):
        assert launch.env[name] == ""
    assert launch.env["DO_NOT_TRACK"] == "1"
    assert launch.env["APP_API_KEY"] == _GATEWAY.api_key
    assert "OPENAI_API_KEY" not in launch.env


def test_protected_run_points_cline_at_the_relay_and_returns_its_answer(tmp_path: Path) -> None:
    """Run the protected command against a fake Cline: the relay URL, gateway token and answer come through."""
    stdout, calls, workspace = _run_with_fake_cline(tmp_path, protected=True)

    auth, run = [line for line in calls.splitlines() if line.startswith("ARGS")]
    assert f"[--baseurl] [{_RELAY}]" in auth
    assert _GATEWAY.url not in calls
    assert "[--apikey] [gateway-token]" in auth
    assert "[-P] [openai] [-k] [gateway-token] [-m] [target-model] [--yolo] [--]" in run
    assert run.endswith("[Fix `solution.py` and say $(echo done).]")
    assert "OPENAI_API_KEY=gateway-token" in calls
    assert "ANTHROPIC_API_KEY=\n" in calls
    data_dir = Path(next(line for line in calls.splitlines() if line.startswith("CLINE_DATA_DIR=")).split("=", 1)[1])
    assert not data_dir.is_relative_to(workspace.resolve())
    assert not data_dir.is_relative_to(workspace)
    state = json.loads((data_dir / "globalState.json").read_text())
    shutil.rmtree(data_dir)
    assert state == {"welcomeViewCompleted": True, "isNewUser": False}
    assert not list(workspace.rglob("globalState.json"))
    assert parse_output("plain", stdout) == (
        "I read solution.py and fixed the off-by-one error.\n\nThe answer is 42.",
        {},
    )


def test_open_run_reads_the_gateway_url_from_the_generated_file(tmp_path: Path) -> None:
    """Without a relay, seed Cline with the gateway URL the launch wrote."""
    _, calls, _ = _run_with_fake_cline(tmp_path, protected=False)

    assert f"[--baseurl] [{_GATEWAY.url}]" in calls


def test_recorded_output_parses_to_the_answer_and_the_payload_names_the_plain_parser() -> None:
    """Parse a recorded transcript to its stripped text, and serialize the launch with the bridge's plain parser."""
    assert parse_plain_output(_RECORDED_STDOUT) == (
        "I read solution.py and fixed the off-by-one error.\n\nThe answer is 42.",
        {},
    )
    assert parse_plain_output("  \n") == (None, {})
    assert launch_payload(build_launch(_target(), _GATEWAY))["output_format"] == "plain"
