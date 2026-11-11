"""Tests for the Qwen Code harness: registry entry, pinned install, gateway-only launch, protected env and output parsing."""

from __future__ import annotations

import os
import shutil
import stat
import subprocess
from pathlib import Path

import pytest

from core.models.blackbox import BLACKBOX_HARNESS_QWEN_CODE, BLACKBOX_HARNESSES, BLACKBOX_TARGET_AGENT, BlackboxTarget

from ..harness import GatewayConfig, build_launch, launch_payload, pinned_harness_check
from ..harness_bridge import parse_output, parse_plain_output
from ..harnesses import REGISTRY
from ..harnesses.qwen_code import _PROTECTED_ENV, QWEN_CODE_BASE_URL_FILE

_GATEWAY = GatewayConfig(url="https://gw.example/v1", api_key="gateway-token")
_RELAY = "http://127.0.0.1:3210/v1"
_PROVIDER_KEYS = ("OPENAI_API_KEY", "DASHSCOPE_API_KEY", "QWEN_API_KEY", "ANTHROPIC_API_KEY", "GEMINI_API_KEY")
_PROMPT = "Fix `solution.py` and say $(echo done)."
# Synthetic one-shot transcript in the shape of ``qwen --prompt`` text output.
_RECORDED_STDOUT = "I read solution.py and fixed the off-by-one error.\n\nThe answer is 42.\n"
# Stand-in for the qwen binary: it records each call and prints a fixed answer.
_FAKE_QWEN = """#!/bin/sh
log="$FAKE_LOG"
{
  printf 'ARGS'; for a in "$@"; do printf ' [%s]' "$a"; done; printf '\\n'
  printf 'OPENAI_BASE_URL=%s\\n' "$OPENAI_BASE_URL"
  printf 'OPENAI_API_KEY=%s\\n' "$OPENAI_API_KEY"
  printf 'OPENAI_MODEL=%s\\n' "$OPENAI_MODEL"
  printf 'DASHSCOPE_API_KEY=%s\\n' "${DASHSCOPE_API_KEY-unset}"
  printf 'QWEN_HOME=%s\\n' "$QWEN_HOME"
  if [ -d "$QWEN_HOME" ]; then echo 'HOME_DIR=yes'; else echo 'HOME_DIR=no'; fi
} >> "$log"
printf '%s' "$FAKE_ANSWER"
exit "${FAKE_EXIT:-0}"
"""


def _target() -> BlackboxTarget:
    """Build a Qwen Code agent target.

    Returns:
        The target.
    """
    return BlackboxTarget(kind=BLACKBOX_TARGET_AGENT, harness=BLACKBOX_HARNESS_QWEN_CODE, model="target-model")


def _run_with_fake_qwen(tmp_path: Path, *, protected: bool, fake_exit: int = 0) -> tuple[str, str, Path, int]:
    """Run the built qwen command in a scratch workspace against a fake ``qwen`` binary.

    The env also carries an inherited ``OPENAI_BASE_URL``, ``OPENAI_API_KEY``
    and ``DASHSCOPE_API_KEY``, which the run must replace or a protected run
    must blank.

    Args:
        tmp_path: Scratch directory for the workspace, the fake binary and its log.
        protected: Build the protected launch, with the sandbox-local relay set.
        fake_exit: Exit status the fake binary returns.

    Returns:
        The command's stdout, the fake binary's call log, the workspace, and
        the command's exit status.
    """
    launch = build_launch(_target(), _GATEWAY, protected=protected)
    workspace = tmp_path / "work"
    for name, content in launch.files.items():
        path = workspace / name
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(content)
    (workspace / "task").mkdir(parents=True)
    (workspace / "task" / "PROMPT.md").write_text(_PROMPT)
    bin_dir = tmp_path / "bin"
    bin_dir.mkdir()
    fake = bin_dir / "qwen"
    fake.write_text(_FAKE_QWEN)
    fake.chmod(fake.stat().st_mode | stat.S_IEXEC)
    log = tmp_path / "calls.log"
    env = {
        "OPENAI_BASE_URL": "https://api.openai.com/v1",
        "OPENAI_API_KEY": "provider-key",
        "DASHSCOPE_API_KEY": "provider-key",
        **launch.env,
        "PATH": f"{bin_dir}{os.pathsep}{os.defpath}",
        "HOME": str(tmp_path / "home"),
        "FAKE_LOG": str(log),
        "FAKE_ANSWER": _RECORDED_STDOUT,
        "FAKE_EXIT": str(fake_exit),
    }
    if protected:
        env["APP_BUDGET_RELAY_URL"] = _RELAY
    # The sandbox runs the command with bash, not /bin/sh.
    result = subprocess.run(
        [shutil.which("bash") or "/bin/sh", "-c", launch.run_command],
        cwd=workspace,
        env=env,
        capture_output=True,
        text=True,
        timeout=10,
        check=False,
    )
    return result.stdout, log.read_text(), workspace, result.returncode


def _logged(calls: str, name: str) -> str:
    """Return the value the fake binary logged for ``name``.

    Args:
        calls: The fake binary's call log.
        name: The logged variable name.

    Returns:
        The logged value.
    """
    return next(line for line in calls.splitlines() if line.startswith(f"{name}=")).split("=", 1)[1]


def test_qwen_code_is_registered_after_aider_with_its_pin() -> None:
    """List qwen_code in the registry and the request model, pinned to the exact npm release."""
    spec = REGISTRY[BLACKBOX_HARNESS_QWEN_CODE]
    assert (spec.binary, spec.version, spec.output_format, spec.position) == ("qwen", "0.27.2", "plain", 58)
    assert list(REGISTRY).index(BLACKBOX_HARNESS_QWEN_CODE) == list(REGISTRY).index("aider") + 1
    assert BLACKBOX_HARNESS_QWEN_CODE in BLACKBOX_HARNESSES
    assert (
        pinned_harness_check(BLACKBOX_HARNESS_QWEN_CODE)
        == "qwen --version 2>/dev/null | grep -Eq '(^| )0[.]27[.]2( |$)'"
    )


def test_open_install_pins_the_npm_release() -> None:
    """Install the exact npm release only when the pinned binary is missing."""
    install = build_launch(_target(), _GATEWAY).install_command

    assert install == f"{pinned_harness_check('qwen_code')} || npm install -g @qwen-code/qwen-code@0.27.2"


def test_open_launch_sends_model_calls_only_to_the_gateway() -> None:
    """Read the gateway URL from a file, pass the gateway token by env only, and keep provider keys out of the env."""
    launch = build_launch(_target(), _GATEWAY)

    assert launch.instructions_file == "AGENTS.md"
    assert launch.files == {QWEN_CODE_BASE_URL_FILE: _GATEWAY.url}
    assert _GATEWAY.url not in launch.run_command
    assert _GATEWAY.api_key not in launch.run_command
    assert "--openai-api-key" not in launch.run_command
    assert "--openai-base-url" not in launch.run_command
    assert (
        f'OPENAI_BASE_URL="$(cat {QWEN_CODE_BASE_URL_FILE})" OPENAI_API_KEY="$APP_API_KEY" OPENAI_MODEL="$APP_MODEL" '
        'qwen --yolo --auth-type openai --model "$APP_MODEL" --prompt "$(cat "$APP_PROMPT_FILE")" < /dev/null'
    ) in launch.run_command
    assert launch.env["APP_API_KEY"] == _GATEWAY.api_key
    assert launch.env["APP_GATEWAY_URL"] == _GATEWAY.url
    assert launch.env["QWEN_TELEMETRY_ENABLED"] == "false"
    assert launch.env["QWEN_USAGE_STATISTICS_ENABLED"] == "false"
    assert launch.env["QWEN_CODE_SKIP_UPDATE_CHECK_ONCE"] == "true"
    assert launch.env["QWEN_CODE_MODELS_DEV"] == "off"
    assert not set(_PROVIDER_KEYS) & set(launch.env)
    assert launch.parse_output is parse_plain_output


@pytest.mark.parametrize("effort", [None, "low", "high", "xhigh"])
def test_reasoning_effort_does_not_change_the_launch(effort: str | None) -> None:
    """Build the same launch for every effort, since Qwen Code 0.27.2 has no effort flag or env variable."""
    assert build_launch(_target(), _GATEWAY, reasoning_effort=effort) == build_launch(_target(), _GATEWAY)


def test_protected_launch_checks_the_pin_offline_and_blocks_other_providers() -> None:
    """Use the offline version probe, blank other provider keys and OAuth, and keep the gateway key."""
    launch = build_launch(_target(), _GATEWAY, protected=True)

    assert launch.install_command == pinned_harness_check(BLACKBOX_HARNESS_QWEN_CODE)
    assert {"QWEN_API_KEY", "DASHSCOPE_API_KEY", "ANTHROPIC_API_KEY", "GEMINI_API_KEY", "QWEN_OAUTH"} <= set(
        _PROTECTED_ENV
    )
    for name, value in _PROTECTED_ENV.items():
        assert value == ""
        assert launch.env[name] == ""
    assert launch.env["QWEN_TELEMETRY_ENABLED"] == "false"
    assert launch.env["APP_API_KEY"] == _GATEWAY.api_key
    assert "OPENAI_API_KEY" not in launch.env


def test_protected_run_points_qwen_at_the_relay_and_returns_its_answer(tmp_path: Path) -> None:
    """Run the protected command against a fake qwen: the relay URL, gateway token, prompt and answer come through."""
    stdout, calls, workspace, code = _run_with_fake_qwen(tmp_path, protected=True)

    assert code == 0
    (run,) = [line for line in calls.splitlines() if line.startswith("ARGS")]
    assert run == (f"ARGS [--yolo] [--auth-type] [openai] [--model] [target-model] [--prompt] [{_PROMPT}]")
    assert _logged(calls, "OPENAI_BASE_URL") == _RELAY
    assert _GATEWAY.url not in calls
    assert _logged(calls, "OPENAI_API_KEY") == "gateway-token"
    assert _logged(calls, "OPENAI_MODEL") == "target-model"
    assert _logged(calls, "DASHSCOPE_API_KEY") == ""
    home = Path(_logged(calls, "QWEN_HOME"))
    assert _logged(calls, "HOME_DIR") == "yes"
    assert not home.exists()
    assert not home.resolve().is_relative_to(workspace.resolve())
    assert parse_output("plain", stdout) == (
        "I read solution.py and fixed the off-by-one error.\n\nThe answer is 42.",
        {},
    )


def test_open_run_reads_the_gateway_url_from_the_generated_file(tmp_path: Path) -> None:
    """Without a relay, point qwen at the gateway URL the launch wrote, over an inherited base URL and key."""
    _, calls, _, _ = _run_with_fake_qwen(tmp_path, protected=False)

    assert _logged(calls, "OPENAI_BASE_URL") == _GATEWAY.url
    assert _logged(calls, "OPENAI_API_KEY") == "gateway-token"


@pytest.mark.parametrize("fake_exit", [0, 3])
def test_run_removes_the_qwen_home_and_keeps_qwen_exit_status_and_output(tmp_path: Path, fake_exit: int) -> None:
    """Remove the per-run QWEN_HOME when the shell exits, after a good or failed run, without changing the result."""
    stdout, calls, _, code = _run_with_fake_qwen(tmp_path, protected=False, fake_exit=fake_exit)

    assert code == fake_exit
    assert stdout == _RECORDED_STDOUT
    assert _logged(calls, "HOME_DIR") == "yes"
    assert not Path(_logged(calls, "QWEN_HOME")).exists()


def test_recorded_output_parses_to_the_answer_and_the_payload_names_the_plain_parser() -> None:
    """Parse a recorded transcript to its stripped text, and serialize the launch with the bridge's plain parser."""
    assert parse_plain_output(_RECORDED_STDOUT) == (
        "I read solution.py and fixed the off-by-one error.\n\nThe answer is 42.",
        {},
    )
    assert parse_plain_output("  \n") == (None, {})
    assert launch_payload(build_launch(_target(), _GATEWAY))["output_format"] == "plain"
