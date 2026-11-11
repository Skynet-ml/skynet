"""Tests for the Grok Build CLI harness: registry entry, gateway-only launch, protected env and output parsing."""

from __future__ import annotations

import json
import os
import shlex
import stat
import subprocess
import tomllib
from pathlib import Path

import pytest

from core.exceptions import ServiceError
from core.models.blackbox import BLACKBOX_HARNESS_GROK_BUILD, BLACKBOX_HARNESSES, BLACKBOX_TARGET_AGENT, BlackboxTarget

from ..harness import GatewayConfig, build_launch, launch_payload, pinned_harness_check
from ..harness_bridge import parse_output, parse_pi_output
from ..harnesses import REGISTRY
from ..harnesses.grok_build import _CONVERTER_SCRIPT, GROK_CONFIG_FILE, GROK_VERSION

_GATEWAY = GatewayConfig(url="https://gw.example/v1", api_key="gateway-token")
_RELAY = "http://127.0.0.1:3210/v1"
_MODEL = "acme/grok-4"
_DIRECT_KEYS = ("XAI_API_KEY", "OPENAI_API_KEY", "ANTHROPIC_API_KEY")
# Synthetic ``grok --output-format streaming-json`` transcript: text chunks, a
# tool call, and the ``end`` event with cache and reasoning buckets.
_RECORDED_STDOUT = "\n".join(
    json.dumps(event)
    for event in (
        {"type": "thought", "data": "Check solution.py first."},
        {"type": "text", "data": "I fixed the off-by-one "},
        {"type": "tool_call", "name": "bash", "input": {"command": "pytest"}},
        {"type": "text", "data": "error.\n\nThe answer is 42.\n"},
        {
            "type": "end",
            "usage": {
                "input_tokens": 100,
                "output_tokens": 40,
                "cache_read_input_tokens": 20,
                "cache_creation_input_tokens": 5,
                "reasoning_tokens": 30,
            },
        },
    )
)
# Stand-in for the Grok binary: it records its call and config, then prints a fixed stream.
_FAKE_GROK = """#!/bin/sh
{
  printf 'ARGS'; for a in "$@"; do printf ' [%s]' "$a"; done; printf '\\n'
  printf 'HOME=%s\\n' "$HOME"
  printf 'XAI_API_KEY=%s\\n' "$XAI_API_KEY"
  printf 'APP_API_KEY=%s\\n' "$APP_API_KEY"
  printf 'GROK_LOG_FILE=%s\\n' "$GROK_LOG_FILE"
} >> "$FAKE_LOG"
cp "$HOME/.grok/config.toml" "$FAKE_CONFIG_COPY"
printf '%s\\n' "$FAKE_STREAM"
"""


def _target() -> BlackboxTarget:
    """Build a Grok Build agent target.

    Returns:
        The target.
    """
    return BlackboxTarget(kind=BLACKBOX_TARGET_AGENT, harness=BLACKBOX_HARNESS_GROK_BUILD, model=_MODEL)


def _run_with_fake_grok(
    tmp_path: Path, *, protected: bool, stream: str = _RECORDED_STDOUT
) -> tuple[str, str, dict, Path]:
    """Run the built Grok command in a scratch workspace against a fake ``grok`` binary.

    Args:
        tmp_path: Scratch directory for the workspace, the fake binary and its log.
        protected: Build the protected launch, with the sandbox-local relay set.
        stream: The NDJSON the fake binary prints.

    Returns:
        The command's stdout, the fake binary's call log, the config Grok saw, and the workspace.
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
    fake = bin_dir / "grok"
    fake.write_text(_FAKE_GROK)
    fake.chmod(fake.stat().st_mode | stat.S_IEXEC)
    log = tmp_path / "calls.log"
    config_copy = tmp_path / "seen.toml"
    env = {
        **launch.env,
        "PATH": f"{bin_dir}{os.pathsep}{os.defpath}",
        "HOME": str(tmp_path / "home"),
        "FAKE_LOG": str(log),
        "FAKE_CONFIG_COPY": str(config_copy),
        "FAKE_STREAM": stream,
    }
    if protected:
        env["APP_BUDGET_RELAY_URL"] = _RELAY
    result = subprocess.run(
        launch.run_command, shell=True, cwd=workspace, env=env, capture_output=True, text=True, timeout=10, check=True
    )
    return result.stdout, log.read_text(), tomllib.loads(config_copy.read_text()), workspace


def test_grok_build_is_registered_before_cline_with_its_pin() -> None:
    """List Grok Build in the registry and the request model, pinned to the exact CLI release."""
    spec = REGISTRY[BLACKBOX_HARNESS_GROK_BUILD]
    assert (spec.binary, spec.version, spec.output_format, spec.position) == ("grok", "1.0.77", "pi", 67)
    ids = list(REGISTRY)
    assert ids.index("antigravity_sdk") < ids.index(BLACKBOX_HARNESS_GROK_BUILD) < ids.index("cline")
    assert BLACKBOX_HARNESS_GROK_BUILD in BLACKBOX_HARNESSES
    assert (
        pinned_harness_check(BLACKBOX_HARNESS_GROK_BUILD)
        == "grok --version 2>/dev/null | grep -Eq '(^| )1[.]0[.]77( |$)'"
    )


def test_open_launch_installs_the_pin_and_sends_model_calls_only_to_the_gateway() -> None:
    """Install the pinned release, route every model role to the gateway in the config, and blank direct keys."""
    launch = build_launch(_target(), _GATEWAY)

    assert launch.instructions_file == "AGENTS.md"
    assert launch.install_command == (
        'export PATH="$HOME/.grok/bin:$PATH"; '
        "grok --version 2>/dev/null | grep -Eq '(^| )1[.]0[.]77( |$)' || "
        f"curl -fsSL https://x.ai/cli/install.sh | bash -s {GROK_VERSION}"
    )
    assert set(launch.files) == {GROK_CONFIG_FILE}
    config = tomllib.loads(launch.files[GROK_CONFIG_FILE])
    assert config["cli"] == {"auto_update": False}
    assert config["features"] == {"telemetry": False}
    assert config["harness"] == {"disable_codebase_upload": True}
    assert set(config["models"].values()) == {_MODEL}
    assert config["model"] == {
        _MODEL: {
            "name": _MODEL,
            "model": _MODEL,
            "base_url": _GATEWAY.url,
            "env_key": "APP_API_KEY",
            "api_backend": "chat_completions",
        }
    }
    assert _GATEWAY.api_key not in "".join(launch.files.values())
    assert _GATEWAY.url not in launch.run_command
    assert _GATEWAY.api_key not in launch.run_command
    assert (
        'grok --no-auto-update --single "$(cat "$APP_PROMPT_FILE")" --always-approve '
        f'--output-format streaming-json --model "$APP_MODEL" < /dev/null | python3 -c {shlex.quote(_CONVERTER_SCRIPT)}'
    ) in launch.run_command
    assert launch.env["APP_API_KEY"] == _GATEWAY.api_key
    assert launch.env["GROK_DISABLE_AUTOUPDATER"] == "1"
    assert all(launch.env[name] == "" for name in _DIRECT_KEYS)
    assert launch.parse_output is parse_pi_output


@pytest.mark.parametrize(
    ("effort", "flag"),
    [(None, None), ("none", "none"), ("minimal", "minimal"), ("high", "high"), ("max", "max"), ("turbo", None)],
)
def test_reasoning_effort_maps_to_the_effort_flag(effort: str | None, flag: str | None) -> None:
    """Pass a known effort as Grok's ``--reasoning-effort``, and no flag for anything else."""
    launch = build_launch(_target(), _GATEWAY, reasoning_effort=effort)
    if flag is None:
        assert "--reasoning-effort" not in launch.run_command
    else:
        assert f'--model "$APP_MODEL" --reasoning-effort {flag} < /dev/null' in launch.run_command


def test_protected_launch_checks_the_pin_offline_and_blocks_other_providers() -> None:
    """Use the offline version probe, blank every direct provider key, and keep the gateway key."""
    launch = build_launch(_target(), _GATEWAY, protected=True)

    assert launch.install_command == pinned_harness_check(BLACKBOX_HARNESS_GROK_BUILD)
    assert all(launch.env[name] == "" for name in _DIRECT_KEYS)
    assert launch.env["GROK_DISABLE_AUTOUPDATER"] == "1"
    assert launch.env["DO_NOT_TRACK"] == "1"
    assert launch.env["APP_API_KEY"] == _GATEWAY.api_key


@pytest.mark.parametrize("url", ["", "gw.example/v1", 'https://gw.example/"v1', "https://user:pw@gw.example/v1"])
def test_launch_refuses_a_gateway_url_that_cannot_sit_in_the_config(url: str) -> None:
    """Stop the launch for a missing, relative, quoted or credentialed gateway URL."""
    with pytest.raises(ServiceError):
        build_launch(_target(), GatewayConfig(url=url, api_key="gateway-token"))


def test_protected_run_points_grok_at_the_relay_and_returns_its_answer(tmp_path: Path) -> None:
    """Run the protected command against a fake Grok: the relay URL, gateway token and answer come through."""
    stdout, calls, config, workspace = _run_with_fake_grok(tmp_path, protected=True)

    run = next(line for line in calls.splitlines() if line.startswith("ARGS"))
    assert run == (
        "ARGS [--no-auto-update] [--single] [Fix `solution.py` and say $(echo done).] [--always-approve] "
        f"[--output-format] [streaming-json] [--model] [{_MODEL}]"
    )
    assert config["model"][_MODEL]["base_url"] == _RELAY
    assert _GATEWAY.url not in calls
    assert "APP_API_KEY=gateway-token" in calls
    assert "XAI_API_KEY=\n" in calls
    grok_home = Path(next(line for line in calls.splitlines() if line.startswith("HOME=")).split("=", 1)[1])
    assert not grok_home.is_relative_to(workspace.resolve())
    assert not grok_home.is_relative_to(workspace)
    assert f"GROK_LOG_FILE={grok_home}/grok.log" in calls
    assert parse_output("pi", stdout) == (
        "I fixed the off-by-one error.\n\nThe answer is 42.",
        {"input_tokens": 125, "output_tokens": 40},
    )


def test_open_run_reads_the_gateway_url_from_the_generated_config(tmp_path: Path) -> None:
    """Without a relay, Grok gets the gateway URL the launch wrote."""
    _, _, config, _ = _run_with_fake_grok(tmp_path, protected=False)

    assert config["model"][_MODEL]["base_url"] == _GATEWAY.url


def test_an_error_stream_without_text_fails_the_run(tmp_path: Path) -> None:
    """Fail the command and surface Grok's message when the stream has an error and no answer."""
    with pytest.raises(subprocess.CalledProcessError) as failure:
        _run_with_fake_grok(tmp_path, protected=False, stream=json.dumps({"type": "error", "message": "bad route"}))

    assert "bad route" in failure.value.stderr


def test_the_payload_names_the_pi_parser() -> None:
    """Serialize the launch with the bridge's Pi parser, which reads the converter's event."""
    assert launch_payload(build_launch(_target(), _GATEWAY))["output_format"] == "pi"
