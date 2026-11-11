"""Tests for the Cline CLI harness: registry entry, gateway-only launch, protected env and output parsing."""

from __future__ import annotations

import json
import os
import shutil
import signal
import stat
import subprocess
import sys
import time
from pathlib import Path

import pytest

from core.exceptions import ServiceError
from core.models.blackbox import (
    BLACKBOX_HARNESS_CLINE,
    BLACKBOX_HARNESSES,
    BLACKBOX_TARGET_AGENT,
    BlackboxProposer,
    BlackboxTarget,
)

from ..harness import GatewayConfig, build_launch, launch_payload, pinned_harness_check
from ..harness_bridge import parse_output, parse_plain_output
from ..harnesses import REGISTRY
from ..harnesses.cline import CLINE_BASE_URL_FILE, CLINE_THINKING_LEVELS, CLINE_VERSION
from ..service import _check_proposer_effort

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
  if [ -f "$CLINE_DATA_DIR/globalState.json" ]; then printf 'STATE=%s\\n' "$(cat "$CLINE_DATA_DIR/globalState.json")"; fi
} >> "$log"
if [ "$1" = "auth" ]; then echo "saved"; exit 0; fi
[ -z "$FAKE_SLEEP" ] || sleep "$FAKE_SLEEP"
printf '%s' "$FAKE_ANSWER"
exit "${FAKE_EXIT:-0}"
"""
_PATH = f"{Path(sys.executable).parent}{os.pathsep}{os.defpath}"


def _target() -> BlackboxTarget:
    """Build a Cline agent target.

    Returns:
        The target.
    """
    return BlackboxTarget(kind=BLACKBOX_TARGET_AGENT, harness=BLACKBOX_HARNESS_CLINE, model="target-model")


def _start_fake_cline(
    tmp_path: Path, tag: str, *, protected: bool = False, fake_exit: int = 0, sleep: int = 0
) -> tuple[subprocess.Popen[str], Path, Path]:
    """Start the built Cline command in its own process group against a fake ``cline`` binary.

    Every run shares one fake binary and one ``TMPDIR``, as runs in one box do.

    Args:
        tmp_path: Scratch directory for the workspaces, the fake binary, the logs and ``TMPDIR``.
        tag: Name of this run's workspace and call log.
        protected: Build the protected launch, with the sandbox-local relay set.
        fake_exit: Exit status the fake binary returns.
        sleep: Seconds the fake binary waits before it answers.

    Returns:
        The running shell, its call log, and its workspace.
    """
    launch = build_launch(_target(), _GATEWAY, protected=protected)
    workspace = tmp_path / f"work-{tag}"
    for name, content in launch.files.items():
        path = workspace / name
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(content)
    (workspace / "task").mkdir(parents=True)
    (workspace / "task" / "PROMPT.md").write_text("Fix `solution.py` and say $(echo done).")
    bin_dir = tmp_path / "bin"
    bin_dir.mkdir(exist_ok=True)
    fake = bin_dir / "cline"
    fake.write_text(_FAKE_CLINE)
    fake.chmod(fake.stat().st_mode | stat.S_IEXEC)
    (tmp_path / "tmp").mkdir(exist_ok=True)
    log = tmp_path / f"calls-{tag}.log"
    env = {
        **launch.env,
        "PATH": f"{bin_dir}{os.pathsep}{_PATH}",
        "HOME": str(tmp_path / "home"),
        "TMPDIR": str(tmp_path / "tmp"),
        "FAKE_LOG": str(log),
        "FAKE_ANSWER": _RECORDED_STDOUT,
        "FAKE_EXIT": str(fake_exit),
        "FAKE_SLEEP": str(sleep or ""),
    }
    if protected:
        env["APP_BUDGET_RELAY_URL"] = _RELAY
    # The sandbox runs the command with bash, not /bin/sh.
    process = subprocess.Popen(
        [shutil.which("bash") or "/bin/sh", "-c", launch.run_command],
        cwd=workspace,
        env=env,
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        text=True,
        start_new_session=True,
    )
    return process, log, workspace


def _run_with_fake_cline(tmp_path: Path, *, protected: bool, fake_exit: int = 0) -> tuple[str, str, Path, int]:
    """Run the built Cline command in a scratch workspace against a fake ``cline`` binary.

    Args:
        tmp_path: Scratch directory for the workspace, the fake binary and its log.
        protected: Build the protected launch, with the sandbox-local relay set.
        fake_exit: Exit status the fake binary returns.

    Returns:
        The command's stdout, the fake binary's call log, the workspace, and
        the command's exit status.
    """
    process, log, workspace = _start_fake_cline(tmp_path, "run", protected=protected, fake_exit=fake_exit)
    stdout, _ = process.communicate(timeout=10)
    return stdout, log.read_text(), workspace, process.returncode


def _data_dir(calls: str) -> Path:
    """Return the Cline data directory the fake binary logged.

    Args:
        calls: The fake binary's call log.

    Returns:
        The run's data directory.
    """
    return Path(next(line for line in calls.splitlines() if line.startswith("CLINE_DATA_DIR=")).split("=", 1)[1])


def test_cline_is_registered_between_goose_and_code_puppy_with_its_pin() -> None:
    """List Cline in the registry and the request model, pinned to the exact npm release."""
    spec = REGISTRY[BLACKBOX_HARNESS_CLINE]
    assert (spec.binary, spec.version, spec.output_format) == ("cline", "3.0.74", "plain")
    order = list(REGISTRY)
    assert order.index("goose") < order.index(BLACKBOX_HARNESS_CLINE) < order.index("code_puppy")
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


def test_supported_levels_are_the_pinned_release_thinking_levels() -> None:
    """Declare exactly the ``--thinking`` levels of Cline 3.0.74, on the spec the submit check reads."""
    assert CLINE_THINKING_LEVELS == ("none", "low", "medium", "high", "xhigh")
    assert REGISTRY[BLACKBOX_HARNESS_CLINE].supported_efforts == CLINE_THINKING_LEVELS


@pytest.mark.parametrize("effort", [None, "none", "low", "medium", "high", "xhigh"])
def test_submit_accepts_the_supported_levels(effort: str | None) -> None:
    """Let a Cline proposer submit with no effort or a level Cline supports."""
    _check_proposer_effort(BlackboxProposer(harness=BLACKBOX_HARNESS_CLINE), effort)


@pytest.mark.parametrize("effort", ["minimal", "max", "High"])
def test_submit_refuses_an_unsupported_level_before_paid_work(effort: str) -> None:
    """Refuse at submit a level Cline does not support, before the run reserves budget."""
    with pytest.raises(ServiceError, match=f"cline proposer does not support the '{effort}' effort level"):
        _check_proposer_effort(BlackboxProposer(harness=BLACKBOX_HARNESS_CLINE), effort)


@pytest.mark.parametrize("effort", ["none", "low", "medium", "high", "xhigh"])
def test_supported_effort_goes_to_the_thinking_flag_unchanged(effort: str) -> None:
    """Pass each level Cline supports as its ``--thinking`` value, with no remap."""
    launch = build_launch(_target(), _GATEWAY, reasoning_effort=effort)
    assert f"--yolo --thinking {effort} -- " in launch.run_command
    assert launch.run_command.count("--thinking") == 1


@pytest.mark.parametrize("effort", [None, ""])
def test_no_effort_keeps_clines_default(effort: str | None) -> None:
    """Send no ``--thinking`` flag when the user picks no effort."""
    launch = build_launch(_target(), _GATEWAY, reasoning_effort=effort)
    assert "--thinking" not in launch.run_command


@pytest.mark.parametrize("effort", ["minimal", "max", "auto", "High", "extreme"])
def test_unsupported_effort_raises_in_build_launch(effort: str) -> None:
    """Refuse in the launch too, as a backstop, instead of a silent remap or drop."""
    with pytest.raises(ServiceError, match=f"Cline does not support the effort level '{effort}'"):
        build_launch(_target(), _GATEWAY, reasoning_effort=effort)


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
    stdout, calls, workspace, code = _run_with_fake_cline(tmp_path, protected=True)

    assert code == 0
    auth, run = [line for line in calls.splitlines() if line.startswith("ARGS")]
    assert f"[--baseurl] [{_RELAY}]" in auth
    assert _GATEWAY.url not in calls
    assert "[--apikey] [gateway-token]" in auth
    assert "[-P] [openai] [-k] [gateway-token] [-m] [target-model] [--yolo] [--]" in run
    assert run.endswith("[Fix `solution.py` and say $(echo done).]")
    assert "OPENAI_API_KEY=gateway-token" in calls
    assert "ANTHROPIC_API_KEY=\n" in calls
    data_dir = _data_dir(calls)
    assert not data_dir.is_relative_to(workspace.resolve())
    assert not data_dir.is_relative_to(workspace)
    state = next(line for line in calls.splitlines() if line.startswith("STATE=")).split("=", 1)[1]
    assert json.loads(state) == {"welcomeViewCompleted": True, "isNewUser": False}
    assert not list(workspace.rglob("globalState.json"))
    assert parse_output("plain", stdout) == (
        "I read solution.py and fixed the off-by-one error.\n\nThe answer is 42.",
        {},
    )


def test_open_run_reads_the_gateway_url_from_the_generated_file(tmp_path: Path) -> None:
    """Without a relay, seed Cline with the gateway URL the launch wrote."""
    _, calls, _, _ = _run_with_fake_cline(tmp_path, protected=False)

    assert f"[--baseurl] [{_GATEWAY.url}]" in calls


@pytest.mark.parametrize("fake_exit", [0, 3])
def test_run_removes_the_data_dir_and_keeps_the_exit_status(tmp_path: Path, fake_exit: int) -> None:
    """Remove the data directory with the saved token after a good or failed run, and keep Cline's exit status (SKY-613)."""
    stdout, calls, _, code = _run_with_fake_cline(tmp_path, protected=False, fake_exit=fake_exit)

    assert code == fake_exit
    assert stdout == _RECORDED_STDOUT
    data_dir = _data_dir(calls)
    assert data_dir.parent == tmp_path / "tmp"
    assert data_dir.name.startswith("app-cline-")
    assert not data_dir.exists()


def test_next_run_removes_the_data_dir_of_a_run_killed_at_its_timeout(tmp_path: Path) -> None:
    """Remove the data directory a SIGKILL left behind when the next run starts (SKY-613).

    The bridge kills a timed-out run's process group with SIGKILL, so the EXIT
    trap does not run and the folder stays until the next run sweeps it.
    """
    killed, killed_log, _ = _start_fake_cline(tmp_path, "killed", sleep=30)
    deadline = time.monotonic() + 10
    while "[--yolo]" not in (killed_log.read_text() if killed_log.exists() else ""):
        assert time.monotonic() < deadline, "the fake cline never started"
        time.sleep(0.05)
    killed_dir = _data_dir(killed_log.read_text())
    os.killpg(killed.pid, signal.SIGKILL)
    killed.communicate(timeout=10)
    assert killed_dir.is_dir()

    after, after_log, _ = _start_fake_cline(tmp_path, "after")
    after.communicate(timeout=10)

    assert after.returncode == 0
    assert not killed_dir.exists()
    assert not _data_dir(after_log.read_text()).exists()


def test_recorded_output_parses_to_the_answer_and_the_payload_names_the_plain_parser() -> None:
    """Parse a recorded transcript to its stripped text, and serialize the launch with the bridge's plain parser."""
    assert parse_plain_output(_RECORDED_STDOUT) == (
        "I read solution.py and fixed the off-by-one error.\n\nThe answer is 42.",
        {},
    )
    assert parse_plain_output("  \n") == (None, {})
    assert launch_payload(build_launch(_target(), _GATEWAY))["output_format"] == "plain"
