"""Tests for the chat's command tools on the Mac app and their shared parts."""

from __future__ import annotations

import os
import subprocess
import sys
import time
from pathlib import Path

import pytest

from core.service_gateway.agents import local_command_runner as runner_module
from core.service_gateway.agents.local_command_runner import (
    DEFAULT_YIELD_MS,
    MAX_YIELD_MS,
    CommandSession,
    LocalCommandRunner,
    clamp_yield_ms,
    cut_output,
    format_result,
    seatbelt_profile,
)

on_mac = pytest.mark.skipif(
    sys.platform != "darwin" or not Path("/usr/bin/sandbox-exec").exists(), reason="needs macOS sandbox-exec"
)


def test_clamp_yield_uses_default_and_cap() -> None:
    """Check that a missing wait is 10 s and a long wait stops at 30 s."""
    assert clamp_yield_ms(None) == DEFAULT_YIELD_MS == 10_000
    assert clamp_yield_ms(120_000) == MAX_YIELD_MS == 30_000
    assert clamp_yield_ms(-5) == 0


def test_cut_output_keeps_head_and_tail() -> None:
    """Check that long output keeps its start and its end."""
    text = "A" * 1000 + "B" * 1000
    cut, original_tokens = cut_output(text, max_output_tokens=100)
    assert cut.startswith("A" * 100)
    assert cut.endswith("B" * 100)
    assert "chars cut" in cut
    assert len(cut) < 500
    assert original_tokens == 500


def test_cut_output_keeps_short_text() -> None:
    """Check that short output stays as it is."""
    assert cut_output("hello", max_output_tokens=100) == ("hello", None)


def test_format_result_for_exit_and_running() -> None:
    """Check the Codex result text for a done and a running command."""
    done = format_result(wall_seconds=1.5, exit_code=0, session_id=None, output="hi\n", max_output_tokens=100)
    assert "Wall time: 1.500 seconds" in done
    assert "Process exited with code 0" in done
    assert done.endswith("Output:\nhi\n")
    running = format_result(wall_seconds=10, exit_code=None, session_id=3, output="", max_output_tokens=100)
    assert "Process running with session ID 3" in running


def test_command_session_returns_only_new_output() -> None:
    """Check that each read gives the output since the last read."""
    session = CommandSession(1)
    session.append("one ")
    assert session.read(0) == ("one ", None)
    session.append("two")
    session.finish(2)
    assert session.read(1000) == ("two", 2)


def test_command_session_bounds_memory() -> None:
    """Check that a flood of output keeps only a head and a tail in memory."""
    session = CommandSession(1, keep_chars=10)
    for _ in range(100):
        session.append("x" * 10)
    session.append("END")
    text, _ = session.read(0)
    assert len(text) < 80
    assert text.startswith("x" * 10)
    assert text.endswith("END")


def test_seatbelt_profile_denies_network_and_writes_outside_workspace() -> None:
    """Check that the profile has no network and writes only in the workspace."""
    profile = seatbelt_profile()
    assert "(deny network*)" in profile
    assert "(deny file-write*)" in profile
    assert '(param "WORKSPACE")' in profile


def test_workdir_must_stay_in_workspace(tmp_path: Path) -> None:
    """Check that a workdir outside the workspace is refused."""
    runner = LocalCommandRunner(tmp_path / "ws")
    result = runner.exec_command("pwd", workdir="../..")
    assert "workdir must stay inside the workspace" in result
    runner.close()


def test_clean_environment_has_no_backend_keys(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    """Check that the command environment holds no backend secret."""
    monkeypatch.setenv("OPENAI_API_KEY", "sk-test")
    env = LocalCommandRunner(tmp_path / "ws").command_env()
    assert "OPENAI_API_KEY" not in env
    assert env["HOME"] == str((tmp_path / "ws").resolve())


@on_mac
def test_runs_a_command_in_the_workspace(tmp_path: Path) -> None:
    """Check that a command runs in the workspace and reports its exit code."""
    workspace = tmp_path / "ws"
    runner = LocalCommandRunner(workspace)
    (workspace / "attachments" / "m1").mkdir(parents=True)
    (workspace / "attachments" / "m1" / "a.txt").write_text("hello file")
    result = runner.exec_command("cat attachments/m1/a.txt; exit 3")
    assert "hello file" in result
    assert "Process exited with code 3" in result
    runner.close()


@on_mac
def test_cannot_write_outside_workspace_or_use_network(tmp_path: Path) -> None:
    """Check that the sandbox blocks a write outside the workspace and the network."""
    outside = tmp_path / "outside.txt"
    runner = LocalCommandRunner(tmp_path / "ws")
    result = runner.exec_command(f"echo x > {outside}; echo written=$?")
    assert not outside.exists()
    assert "written=1" in result
    net = runner.exec_command("/usr/bin/curl -s -m 3 http://1.1.1.1 > /dev/null; echo net=$?")
    assert "net=" in net
    assert "net=0" not in net
    runner.close()


@on_mac
def test_long_command_gives_session_and_stdin(tmp_path: Path) -> None:
    """Check that a slow command returns a session id and takes stdin."""
    runner = LocalCommandRunner(tmp_path / "ws")
    first = runner.exec_command("read line; echo got=$line", yield_time_ms=300)
    assert "Process running with session ID" in first
    session_id = int(first.split("session ID ")[1].split()[0])
    second = runner.write_stdin(session_id, chars="abc\n", yield_time_ms=5000)
    assert "got=abc" in second
    assert "Process exited with code 0" in second
    assert "Unknown session" in runner.write_stdin(session_id, yield_time_ms=10)
    runner.close()


def test_prunes_oldest_session_and_keeps_newest(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    """Check that at the limit the oldest session ends and the 8 newest stay."""
    runner = LocalCommandRunner(tmp_path / "ws")
    ended: list[int] = []
    monkeypatch.setattr(runner_module, "MAX_SESSIONS", 10)
    for session_id in range(1, 11):
        session = CommandSession(session_id)
        session.started = time.monotonic() + session_id
        runner._sessions[session_id] = session
    monkeypatch.setattr(runner, "_end", lambda session: ended.append(session.id))
    runner._prune()
    assert ended == [1]


def test_python_root_never_opens_the_home(tmp_path: Path) -> None:
    """Check that a Python folder that holds the home, or sits directly in it, falls back to the workspace."""
    home, workspace = str(tmp_path / "home"), str(tmp_path / "home" / "ws")
    assert runner_module._python_root("/", home, workspace) == workspace
    assert runner_module._python_root(f"{home}/.venv", home, workspace) == workspace
    assert runner_module._python_root(f"{home}/Library/App/venv", home, workspace) == f"{home}/Library/App/venv"
    assert runner_module._python_root("/opt/homebrew/opt/python", home, workspace) == "/opt/homebrew/opt/python"


@on_mac
def test_python_readers_start_in_the_sandbox(tmp_path: Path) -> None:
    """Check that the backend's Python and its file readers can start under the profile."""
    runner = LocalCommandRunner(tmp_path / "ws")
    result = runner.exec_command("python3 -c 'import pypdf, xlrd, fastavro; print(\"readers ok\")'")
    assert "readers ok" in result
    runner.close()


@on_mac
def test_close_ends_a_background_child(tmp_path: Path) -> None:
    """Check that closing the runner ends a child the shell left running."""
    marker = f"sleep {300 + os.getpid() % 97}"
    runner = LocalCommandRunner(tmp_path / "ws")
    runner.exec_command(f"{marker} & echo started", yield_time_ms=500)
    runner.close()
    deadline = time.monotonic() + 5
    while time.monotonic() < deadline:
        if subprocess.run(["pgrep", "-f", marker], capture_output=True, check=False).returncode != 0:
            break
        time.sleep(0.05)
    assert subprocess.run(["pgrep", "-f", marker], capture_output=True, check=False).returncode != 0
