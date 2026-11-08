"""Tests for the Code Puppy harness: registry entry, gateway-only launch, protected env and output parsing."""

from __future__ import annotations

import json
import os
import stat
import subprocess
from pathlib import Path

import pytest

from core.models.blackbox import BLACKBOX_HARNESS_CODE_PUPPY, BLACKBOX_HARNESSES, BLACKBOX_TARGET_AGENT, BlackboxTarget

from ..harness import GatewayConfig, build_launch, launch_payload, pinned_harness_check
from ..harness_bridge import parse_output, parse_plain_output
from ..harnesses import REGISTRY
from ..harnesses.code_puppy import (
    CODE_PUPPY_CONFIG_FILE,
    CODE_PUPPY_MODELS_FILE,
    CODE_PUPPY_USAGE_FILE,
    CODE_PUPPY_VERSION,
)

_GATEWAY = GatewayConfig(url="https://gw.example/v1", api_key="gateway-token")
_RELAY = "http://127.0.0.1:3210/v1"
_PROVIDER_KEYS = ("OPENAI_API_KEY", "ANTHROPIC_API_KEY", "GEMINI_API_KEY")
# Synthetic one-shot transcript in the shape of ``code-puppy --prompt`` plain output.
_RECORDED_STDOUT = "\nI read solution.py and fixed the off-by-one error.\n\nThe answer is 42.\n\n"
# Stand-in for Code Puppy: it records its call, checks the seeded config, and
# writes the usage file unless FAKE_FAIL is set (like a swallowed model error).
_FAKE_CODE_PUPPY = """#!/bin/sh
{
  printf 'ARGS'; for a in "$@"; do printf ' [%s]' "$a"; done; printf '\\n'
  printf 'CODE_PUPPY_API_KEY=%s\\n' "$CODE_PUPPY_API_KEY"
  printf 'OPENAI_API_KEY=%s\\n' "$OPENAI_API_KEY"
  printf 'ANTHROPIC_API_KEY=%s\\n' "$ANTHROPIC_API_KEY"
  printf 'GEMINI_API_KEY=%s\\n' "$GEMINI_API_KEY"
  printf 'NO_VERSION_UPDATE=%s\\n' "$NO_VERSION_UPDATE"
  printf 'CFG=%s\\n' "$XDG_CONFIG_HOME/code_puppy/puppy.cfg"
  printf 'MODELS=%s\\n' "$XDG_DATA_HOME/code_puppy/extra_models.json"
} >> "$FAKE_LOG"
[ -s "$XDG_CONFIG_HOME/code_puppy/puppy.cfg" ] || exit 3
[ -s "$XDG_DATA_HOME/code_puppy/extra_models.json" ] || exit 4
printf '%s' "$FAKE_ANSWER"
if [ -z "$FAKE_FAIL" ]; then
  while [ "$#" -gt 0 ]; do
    if [ "$1" = "--usage-file" ]; then printf '{"requests": 1}' > "$2"; fi
    shift
  done
fi
exit 0
"""


def _target() -> BlackboxTarget:
    """Build a Code Puppy agent target.

    Returns:
        The target.
    """
    return BlackboxTarget(kind=BLACKBOX_TARGET_AGENT, harness=BLACKBOX_HARNESS_CODE_PUPPY, model="target-model")


def _run_with_fake_code_puppy(
    tmp_path: Path, *, protected: bool, prompt: str = "Fix `solution.py` and say $(echo done).", fail: bool = False
) -> tuple[subprocess.CompletedProcess[str], str, Path]:
    """Run the built Code Puppy command in a scratch workspace against a fake ``code-puppy`` binary.

    Args:
        tmp_path: Scratch directory for the workspace, the fake binary and its log.
        protected: Build the protected launch, with the sandbox-local relay set.
        prompt: Task prompt written to the prompt file.
        fail: Make the fake skip the usage file, as Code Puppy does after a model error.

    Returns:
        The finished process, the fake binary's call log, and the workspace.
    """
    launch = build_launch(_target(), _GATEWAY, protected=protected)
    workspace = tmp_path / "work"
    for name, content in launch.files.items():
        path = workspace / name
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(content)
    (workspace / "task").mkdir(parents=True)
    (workspace / "task" / "PROMPT.md").write_text(prompt)
    bin_dir = tmp_path / "bin"
    bin_dir.mkdir()
    fake = bin_dir / "code-puppy"
    fake.write_text(_FAKE_CODE_PUPPY)
    fake.chmod(fake.stat().st_mode | stat.S_IEXEC)
    log = tmp_path / "calls.log"
    env = {
        **launch.env,
        "PATH": f"{bin_dir}{os.pathsep}{os.defpath}",
        "HOME": str(tmp_path / "home"),
        "FAKE_LOG": str(log),
        "FAKE_ANSWER": _RECORDED_STDOUT,
    }
    if fail:
        env["FAKE_FAIL"] = "1"
    if protected:
        env["APP_BUDGET_RELAY_URL"] = _RELAY
    result = subprocess.run(
        launch.run_command, shell=True, cwd=workspace, env=env, capture_output=True, text=True, timeout=10, check=False
    )
    return result, log.read_text(), workspace


def test_code_puppy_is_registered_last_with_its_pin() -> None:
    """List Code Puppy in the registry and the request model, pinned to the exact PyPI release."""
    spec = REGISTRY[BLACKBOX_HARNESS_CODE_PUPPY]
    assert (spec.binary, spec.version, spec.output_format) == ("code-puppy", "0.0.967", "plain")
    assert list(REGISTRY)[-1] == BLACKBOX_HARNESS_CODE_PUPPY
    assert BLACKBOX_HARNESSES[-2:] == (BLACKBOX_HARNESS_CODE_PUPPY, "custom")
    assert (
        pinned_harness_check(BLACKBOX_HARNESS_CODE_PUPPY)
        == "code-puppy --version 2>/dev/null | grep -Eq '(^| )0[.]0[.]967( |$)'"
    )


def test_open_launch_installs_the_pin_and_sends_model_calls_only_to_the_gateway() -> None:
    """Install the pinned package, point the only model at the gateway, and blank every provider key."""
    launch = build_launch(_target(), _GATEWAY)

    assert launch.instructions_file == "AGENTS.md"
    assert launch.install_command is not None
    assert f"uv tool install --force --python 3.13 code-puppy=={CODE_PUPPY_VERSION}" in launch.install_command
    assert "astral.sh/uv/0.13.7/install.sh" in launch.install_command
    assert set(launch.files) == {CODE_PUPPY_MODELS_FILE}
    assert json.loads(launch.files[CODE_PUPPY_MODELS_FILE]) == {
        "app": {
            "type": "custom_openai",
            "name": "target-model",
            "custom_endpoint": {"url": _GATEWAY.url, "api_key": "$CODE_PUPPY_API_KEY"},
        }
    }
    for content in launch.files.values():
        assert _GATEWAY.api_key not in content
    assert _GATEWAY.url not in launch.run_command
    assert _GATEWAY.api_key not in launch.run_command
    assert (
        'CODE_PUPPY_API_KEY="$APP_API_KEY" code-puppy --disable-ask-user-question '
        f'--usage-file {CODE_PUPPY_USAGE_FILE} --model app --prompt "$_cp_prompt" < /dev/null'
    ) in launch.run_command
    assert launch.env["APP_API_KEY"] == _GATEWAY.api_key
    assert {name: launch.env[name] for name in _PROVIDER_KEYS} == dict.fromkeys(_PROVIDER_KEYS, "")
    assert "CODE_PUPPY_API_KEY" not in launch.env
    assert launch.parse_output is parse_plain_output


@pytest.mark.parametrize(
    ("effort", "line"),
    [
        (None, None),
        ("none", None),
        ("high", "model_settings_app_reasoning_effort = high"),
        ("max", "model_settings_app_reasoning_effort = max"),
        ("minimal", "model_settings_app_reasoning_effort = minimal"),
    ],
)
def test_reasoning_effort_goes_to_the_config_only_when_code_puppy_knows_it(
    tmp_path: Path, effort: str | None, line: str | None
) -> None:
    """Write the user's effort to ``puppy.cfg`` for Code Puppy's effort names, and nothing otherwise."""
    launch = build_launch(_target(), _GATEWAY, reasoning_effort=effort)
    subprocess.run(
        launch.run_command,
        shell=True,
        cwd=tmp_path,
        env={"PATH": os.defpath},
        capture_output=True,
        timeout=10,
        check=False,
    )
    cfg = (tmp_path / CODE_PUPPY_CONFIG_FILE).read_text()
    assert cfg.startswith(
        "[puppy]\npuppy_name = Puppy\nowner_name = User\nauto_save_session = false\nyolo_mode = true\n"
    )
    if line is None:
        assert "reasoning_effort" not in cfg
    else:
        assert cfg.endswith(f"{line}\n")


def test_protected_launch_checks_the_pin_offline_and_blocks_other_providers() -> None:
    """Use the offline version probe, blank every other provider key, and stop the update check."""
    launch = build_launch(_target(), _GATEWAY, protected=True)

    assert launch.install_command == pinned_harness_check(BLACKBOX_HARNESS_CODE_PUPPY)
    for name in _PROVIDER_KEYS:
        assert launch.env[name] == ""
    assert launch.env["NO_VERSION_UPDATE"] == "1"
    assert launch.env["DO_NOT_TRACK"] == "1"
    assert launch.env["APP_API_KEY"] == _GATEWAY.api_key


def test_protected_run_points_code_puppy_at_the_relay_and_returns_its_answer(tmp_path: Path) -> None:
    """Run the protected command against a fake Code Puppy: the relay URL, gateway token and answer come through."""
    result, calls, workspace = _run_with_fake_code_puppy(tmp_path, protected=True)

    assert result.returncode == 0, result.stderr
    (run,) = [line for line in calls.splitlines() if line.startswith("ARGS")]
    assert run == (
        f"ARGS [--disable-ask-user-question] [--usage-file] [{CODE_PUPPY_USAGE_FILE}] [--model] [app] "
        "[--prompt] [Fix `solution.py` and say $(echo done).]"
    )
    assert "CODE_PUPPY_API_KEY=gateway-token" in calls
    for name in _PROVIDER_KEYS:
        assert f"{name}=\n" in calls
    assert "NO_VERSION_UPDATE=1" in calls
    assert f"CFG={workspace}/{CODE_PUPPY_CONFIG_FILE}" in calls
    assert f"MODELS={workspace}/{CODE_PUPPY_MODELS_FILE}" in calls
    models = json.loads((workspace / CODE_PUPPY_MODELS_FILE).read_text())
    assert models["app"]["custom_endpoint"]["url"] == _RELAY
    assert parse_output("plain", result.stdout) == (
        "I read solution.py and fixed the off-by-one error.\n\nThe answer is 42.",
        {},
    )


def test_open_run_keeps_the_gateway_url(tmp_path: Path) -> None:
    """Without a relay, leave the gateway URL the launch wrote in the model file."""
    result, _, workspace = _run_with_fake_code_puppy(tmp_path, protected=False)

    assert result.returncode == 0, result.stderr
    models = json.loads((workspace / CODE_PUPPY_MODELS_FILE).read_text())
    assert models["app"]["custom_endpoint"]["url"] == _GATEWAY.url


@pytest.mark.parametrize("prompt", ["/help me fix it", "  !rm -rf build"])
def test_command_like_prompts_are_framed_as_a_task(tmp_path: Path, prompt: str) -> None:
    """Prefix a prompt that Code Puppy would read as a slash or shell command, so it reaches the model."""
    _, calls, _ = _run_with_fake_code_puppy(tmp_path, protected=False, prompt=prompt)

    assert f"[--prompt] [Task request:\n{prompt}]" in calls


def test_run_fails_when_code_puppy_writes_no_usage_file(tmp_path: Path) -> None:
    """Turn Code Puppy's exit 0 after a swallowed model error into a failed run."""
    result, _, _ = _run_with_fake_code_puppy(tmp_path, protected=False, fail=True)

    assert result.returncode == 1
    assert f"{CODE_PUPPY_USAGE_FILE} was not written" in result.stderr


def test_recorded_output_parses_to_the_answer_and_the_payload_names_the_plain_parser() -> None:
    """Parse a recorded transcript to its stripped text, and serialize the launch with the bridge's plain parser."""
    assert parse_plain_output(_RECORDED_STDOUT) == (
        "I read solution.py and fixed the off-by-one error.\n\nThe answer is 42.",
        {},
    )
    assert parse_plain_output("  \n") == (None, {})
    assert launch_payload(build_launch(_target(), _GATEWAY))["output_format"] == "plain"
