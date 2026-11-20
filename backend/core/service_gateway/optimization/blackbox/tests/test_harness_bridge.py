"""Tests for the ``claude``-compatible bridge that drives any harness for the upstream engines."""

from __future__ import annotations

import base64
import contextlib
import fcntl
import hashlib
import http.server
import json
import os
import re
import shlex
import shutil
import signal
import subprocess
import sys
import threading
import time
from collections.abc import Mapping
from pathlib import Path
from types import SimpleNamespace
from typing import Any

import pytest

from core.billing import guest_model_proxy

from .. import exec_shim, harness, harness_bridge, repo_tree
from ..harness_bridge import (
    SessionOutcome,
    _cli_parser,
    install_shim,
    record_transcript,
    result_document,
    run_session,
)

_PLAIN = {"run_command": 'cat "$ASCENT_TASK"', "output_format": "plain", "files": {}, "env": {}}


def test_cli_parser_accepts_the_meta_harness_and_autoresearch_shapes() -> None:
    """Parse both upstream argv layouts without leaving unknown arguments behind."""
    meta, unknown = _cli_parser().parse_known_args(
        [
            "--print",
            "do the work",
            "--output-format",
            "json",
            "--model",
            "m1",
            "--session-id",
            "s1",
            "--disallowedTools=WebFetch,WebSearch",
            "--permission-mode",
            "bypassPermissions",
            "--effort",
            "high",
            "--max-budget-usd",
            "1.25",
        ]
    )
    assert unknown == []
    assert meta.prompt == ["do the work"]
    assert (meta.model, meta.session_id, meta.effort, meta.max_budget_usd) == ("m1", "s1", "high", "1.25")
    research, unknown = _cli_parser().parse_known_args(
        ["-p", "--output-format", "json", "--resume", "s1", "--settings", "{}", "--model", "m1", "continue"]
    )
    assert unknown == []
    assert research.resume == "s1"
    assert research.prompt == ["continue"]


def test_run_session_writes_files_restores_the_key_and_parses_the_answer(tmp_path: Path) -> None:
    """Materialize the launch in the workspace, swap in the real key and capture the harness answer.

    Args:
        tmp_path: The workspace and the session folder.
    """
    proposer = {
        **_PLAIN,
        "run_command": 'cat "$ASCENT_TASK"; cat config.json; echo "$TOKEN"',
        "files": {"config.json": json.dumps({"key": harness_bridge.KEY_TOKEN})},
        "env": {"TOKEN": harness_bridge.KEY_TOKEN},
        "instructions_file": "AGENTS.md",
    }
    workspace = tmp_path / "work"
    os.environ[harness_bridge.KEY_ENV] = "real-key"
    try:
        outcome = run_session(
            proposer, workspace=workspace, prompt="hello", model="m1", session_dir=tmp_path / "session"
        )
    finally:
        del os.environ[harness_bridge.KEY_ENV]
    assert outcome.returncode == 0
    assert outcome.text == 'hello{"key": "real-key"}real-key'
    assert (workspace / "AGENTS.md").read_text().startswith("#")
    assert json.loads((workspace / "config.json").read_text()) == {"key": "real-key"}
    assert (tmp_path / "session" / "prompt-1.md").read_text() == "hello"
    assert (tmp_path / "session" / "run-1.stdout").exists()


def test_run_session_reports_timeouts_and_budget_overruns(tmp_path: Path) -> None:
    """Kill a harness that outlives its timeout and stop one whose priced usage exceeds the budget.

    Args:
        tmp_path: Pytest's temporary folder.
    """
    slow = {**_PLAIN, "run_command": "sleep 5"}
    outcome = run_session(
        slow, workspace=tmp_path / "w1", prompt="x", model="m", session_dir=tmp_path / "s1", timeout_seconds=0.2
    )
    assert outcome.timed_out is True
    assert outcome.returncode != 0
    event = json.dumps(
        {"type": "message_end", "message": {"role": "assistant", "content": [], "usage": {"input": 1000, "output": 0}}}
    )
    spender = {
        **_PLAIN,
        "output_format": "pi",
        "price": {"input": 0.01, "output": 0.0},
        "run_command": f"echo '{event}'; sleep 5; echo late",
    }
    outcome = run_session(
        spender, workspace=tmp_path / "w2", prompt="x", model="m", session_dir=tmp_path / "s2", max_cost_usd=1.0
    )
    assert outcome.budget_exceeded is True
    assert outcome.usage == {"input_tokens": 1000, "output_tokens": 0}
    assert outcome.cost_usd == pytest.approx(10.0)
    assert outcome.duration_seconds < 4


def _record_signals(
    monkeypatch: pytest.MonkeyPatch, table: dict[int, tuple[int, int, int]]
) -> list[tuple[str, int, int]]:
    """Fake the process table and record each signal ``_stop_group`` sends.

    Args:
        monkeypatch: Pytest monkeypatch fixture.
        table: ``(parent, group, start)`` for each fake pid.

    Returns:
        The list that collects ``("group", pgid, signum)`` and ``("pid", pid, signum)`` entries.
    """
    sent: list[tuple[str, int, int]] = []
    monkeypatch.setattr(harness_bridge.os, "killpg", lambda pid, signum: sent.append(("group", pid, signum)))
    monkeypatch.setattr(harness_bridge.os, "kill", lambda pid, signum: sent.append(("pid", pid, signum)))
    monkeypatch.setattr(harness_bridge, "_proc_stats", lambda: dict(table))
    return sent


def test_stop_group_signals_the_group_once_and_each_detached_descendant(monkeypatch: pytest.MonkeyPatch) -> None:
    """Signal the shell's group once, then only the descendants that left it, never a group member twice.

    Args:
        monkeypatch: Pytest monkeypatch.
    """
    sent = _record_signals(
        monkeypatch,
        {
            100: (1, 100, 7),  # the harness shell
            101: (100, 100, 8),  # a group member, reached by killpg
            102: (101, 102, 9),  # a detached tool command
            103: (100, 103, 9),  # an orphaned background job the shell adopted
            104: (102, 102, 9),  # a child in the tool command's group
            200: (1, 200, 3),  # an unrelated process
        },
    )

    harness_bridge._stop_group(SimpleNamespace(pid=100), signal.SIGTERM, 7)  # type: ignore[arg-type]

    assert sent[0] == ("group", 100, signal.SIGTERM)
    assert sorted(sent[1:]) == [("pid", pid, signal.SIGTERM) for pid in (102, 103, 104)]


def test_stop_group_leaves_a_process_that_reused_the_shell_pid_alone(monkeypatch: pytest.MonkeyPatch) -> None:
    """Send nothing when the reaped shell's pid now belongs to a process with another start time.

    Args:
        monkeypatch: Pytest monkeypatch.
    """
    sent = _record_signals(monkeypatch, {100: (1, 100, 50), 101: (100, 101, 51)})

    harness_bridge._stop_group(SimpleNamespace(pid=100), signal.SIGKILL, 7)  # type: ignore[arg-type]

    assert sent == []


def test_stop_group_signals_the_group_when_the_reaped_pid_is_free(monkeypatch: pytest.MonkeyPatch) -> None:
    """Signal only the group when the shell is gone and nothing took its pid, as members can still hold the pipe.

    Args:
        monkeypatch: Pytest monkeypatch.
    """
    sent = _record_signals(monkeypatch, {101: (1, 100, 8)})

    harness_bridge._stop_group(SimpleNamespace(pid=100), signal.SIGKILL, 7)  # type: ignore[arg-type]

    assert sent == [("group", 100, signal.SIGKILL)]


@pytest.mark.skipif(not Path("/proc/self/stat").exists(), reason="the descendant sweep reads /proc")
def test_stop_group_kills_a_descendant_in_its_own_session(tmp_path: Path) -> None:
    """Kill a grandchild that left the harness group and session, the way eve's sandbox starts a tool command.

    Args:
        tmp_path: Pytest's temporary folder.
    """
    pid_file = tmp_path / "job.pid"
    detached = f"import os, time; os.setsid(); open({str(pid_file)!r}, 'w').write(str(os.getpid())); time.sleep(60)"
    process = subprocess.Popen(
        ["bash", "-c", f"{shlex.quote(sys.executable)} -c {shlex.quote(detached)} & wait"],
        start_new_session=True,
        text=True,
    )
    root_start = harness_bridge._start_time(process.pid)
    deadline = time.monotonic() + 10
    while not (pid_file.exists() and pid_file.read_text().strip()):
        assert time.monotonic() < deadline
        time.sleep(0.05)
    job = int(pid_file.read_text())
    try:
        assert os.getsid(job) != process.pid

        harness_bridge._stop_group(process, signal.SIGKILL, root_start)
        process.wait(timeout=10)

        deadline = time.monotonic() + 10
        # The killed job's parent is gone, so init reaps it; until then it is a zombie.
        while _alive(job):
            assert time.monotonic() < deadline, "the detached job outlived the harness"
            time.sleep(0.05)
    finally:
        with contextlib.suppress(ProcessLookupError):
            os.kill(job, signal.SIGKILL)


# PR_SET_CHILD_SUBREAPER is a Linux prctl, so macOS has no way to keep an
# orphan in the harness tree; the bridge starts the shell plainly there.
@pytest.mark.skipif(sys.platform != "linux", reason="PR_SET_CHILD_SUBREAPER is Linux-only")
def test_timeout_kills_a_background_job_that_a_finished_tool_command_left(tmp_path: Path) -> None:
    """Kill an orphaned ``sleep`` whose detached parent already exited, the way ``sleep 300 &`` in an eve tool does.

    Args:
        tmp_path: Pytest's temporary folder.
    """
    pid_file = tmp_path / "job.pid"
    tool = (
        "import os, subprocess; os.setsid(); "
        "job = subprocess.Popen(['sleep', '300'], stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL); "
        f"open({str(pid_file)!r}, 'w').write(str(job.pid))"
    )
    command = f"{shlex.quote(sys.executable)} -c {shlex.quote(tool)}; sleep 60"
    job = None
    try:
        outcome = run_session(
            {"run_command": command, "output_format": "plain"},
            workspace=tmp_path / "work",
            prompt="x",
            model="m",
            session_dir=tmp_path / "session",
            timeout_seconds=2,
        )
        assert outcome.timed_out
        job = int(pid_file.read_text())
        deadline = time.monotonic() + 10
        while _alive(job):
            assert time.monotonic() < deadline, "the orphaned background job outlived the harness"
            time.sleep(0.05)
    finally:
        if job is not None:
            with contextlib.suppress(ProcessLookupError):
                os.kill(job, signal.SIGKILL)


@pytest.mark.skipif(sys.platform != "linux", reason="PR_SET_CHILD_SUBREAPER is Linux-only")
def test_normal_end_kills_a_background_job_that_a_detached_tool_command_left(tmp_path: Path) -> None:
    """Kill an orphaned ``sleep`` from a detached tool when the harness ends on its own, with no stop.

    Args:
        tmp_path: Pytest's temporary folder.
    """
    pid_file = tmp_path / "job.pid"
    tool = (
        "import os, subprocess; os.setsid(); "
        "job = subprocess.Popen(['sleep', '300'], stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL); "
        f"open({str(pid_file)!r}, 'w').write(str(job.pid))"
    )
    job = None
    try:
        outcome = run_session(
            {"run_command": f"{shlex.quote(sys.executable)} -c {shlex.quote(tool)}", "output_format": "plain"},
            workspace=tmp_path / "work",
            prompt="x",
            model="m",
            session_dir=tmp_path / "session",
            timeout_seconds=30,
        )
        assert outcome.returncode == 0
        assert not outcome.timed_out
        job = int(pid_file.read_text())
        deadline = time.monotonic() + 10
        while _alive(job):
            assert time.monotonic() < deadline, "the orphaned background job outlived the session"
            time.sleep(0.05)
    finally:
        if job is not None:
            with contextlib.suppress(ProcessLookupError):
                os.kill(job, signal.SIGKILL)


@pytest.mark.skipif(not Path("/proc/self/stat").exists(), reason="the normal-end group kill needs /proc")
def test_normal_end_kills_a_background_job_left_in_the_harness_group(tmp_path: Path) -> None:
    """Kill a ``sleep &`` that the harness shell left in its own group when the shell ends on its own.

    Args:
        tmp_path: Pytest's temporary folder.
    """
    pid_file = tmp_path / "job.pid"
    outcome = run_session(
        {
            "run_command": f"sleep 300 >/dev/null 2>&1 & echo $! > {shlex.quote(str(pid_file))}",
            "output_format": "plain",
        },
        workspace=tmp_path / "work",
        prompt="x",
        model="m",
        session_dir=tmp_path / "session",
        timeout_seconds=30,
    )
    job = int(pid_file.read_text())
    try:
        assert outcome.returncode == 0
        deadline = time.monotonic() + 10
        while _running(job):
            assert time.monotonic() < deadline, "the background job outlived the session"
            time.sleep(0.05)
    finally:
        with contextlib.suppress(ProcessLookupError):
            os.kill(job, signal.SIGKILL)


@pytest.mark.skipif(sys.platform != "linux", reason="the reaper entry point runs only on Linux")
def test_harness_pipelines_end_when_the_reader_closes_the_pipe(tmp_path: Path) -> None:
    """Keep SIGPIPE at its default in the harness shell, so ``producer | head -1`` ends at once.

    Args:
        tmp_path: Pytest's temporary folder.
    """
    outcome = run_session(
        {
            "run_command": "while true; do echo x; done | head -1; yes | head -1; echo status=$?",
            "output_format": "plain",
        },
        workspace=tmp_path / "work",
        prompt="x",
        model="m",
        session_dir=tmp_path / "session",
        timeout_seconds=20,
    )

    assert not outcome.timed_out
    assert outcome.returncode == 0
    assert outcome.stdout.splitlines() == ["x", "y", "status=0"]
    assert "Broken pipe" not in outcome.stderr
    assert outcome.duration_seconds < 10


@pytest.mark.skipif(sys.platform != "linux", reason="the reaper entry point runs only on Linux")
def test_cap_stop_lets_the_harness_exit_trap_remove_its_temp_folder(tmp_path: Path) -> None:
    """Let the shell's EXIT trap finish after a tool-call cap stop, so a large temp folder is gone.

    Args:
        tmp_path: Pytest's temporary folder.
    """
    scratch = tmp_path / "scratch"
    for index in range(20):
        part = scratch / str(index)
        part.mkdir(parents=True)
        for name in range(1000):
            (part / str(name)).touch()
    command = f"trap 'rm -rf {shlex.quote(str(scratch))}' EXIT; {_tool_events_command(3)}"
    outcome = run_session(
        {**_PLAIN, "output_format": "pi", "run_command": command, "max_tool_calls": 1},
        workspace=tmp_path / "work",
        prompt="x",
        model="m",
        session_dir=tmp_path / "session",
        timeout_seconds=30,
    )

    assert outcome.tool_cap_reached is True
    assert not scratch.exists()


@pytest.mark.skipif(sys.platform != "linux", reason="the reaper entry point runs only on Linux")
def test_cap_stop_kills_a_shell_that_ignores_sigterm_without_a_timeout(tmp_path: Path) -> None:
    """End a cap stop after the reaper's grace when the shell ignores SIGTERM and the session has no timeout.

    Args:
        tmp_path: Pytest's temporary folder.
    """
    events = _tool_events_command(3).replace("sleep 5", "sleep 120")
    outcome = run_session(
        {**_PLAIN, "output_format": "pi", "run_command": f"trap '' TERM; {events}", "max_tool_calls": 1},
        workspace=tmp_path / "work",
        prompt="x",
        model="m",
        session_dir=tmp_path / "session",
        timeout_seconds=None,
    )

    assert outcome.tool_cap_reached is True
    assert outcome.duration_seconds < 60
    assert not (tmp_path / "work" / "late.txt").exists()


def test_normal_end_sends_no_group_kill_when_the_reaped_pid_cannot_be_checked(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Skip the normal-end group kill without a start time, as the reaped shell pid can belong to a new process.

    Args:
        tmp_path: Pytest's temporary folder.
        monkeypatch: Pytest monkeypatch.
    """
    stops: list[int] = []
    monkeypatch.setattr(harness_bridge, "_start_time", lambda pid: None)
    monkeypatch.setattr(harness_bridge, "_stop_group", lambda process, signum, root_start: stops.append(signum))

    outcome = run_session(
        _PLAIN, workspace=tmp_path / "work", prompt="x", model="m", session_dir=tmp_path / "session", timeout_seconds=30
    )

    assert outcome.returncode == 0
    assert stops == []


def _running(pid: int) -> bool:
    """Tell whether a process runs and is not a zombie, on Linux and macOS.

    Args:
        pid: Process id.

    Returns:
        ``True`` while ``/proc`` or ``ps`` shows the process in a state other than zombie.
    """
    if Path("/proc/self/stat").exists():
        # Slim Linux images can lack ``ps``.
        return _alive(pid)
    state = subprocess.run(["ps", "-o", "stat=", "-p", str(pid)], capture_output=True, text=True, check=False)
    return bool(state.stdout.strip()) and not state.stdout.strip().startswith("Z")


def _alive(pid: int) -> bool:
    """Tell whether a process runs and is not a zombie.

    Args:
        pid: Process id.

    Returns:
        ``True`` while ``/proc`` shows the process in a state other than zombie.
    """
    try:
        return Path(f"/proc/{pid}/stat").read_text().rsplit(")", 1)[1].split()[0] != "Z"
    # Linux raises ProcessLookupError when the process ends between the open and the read.
    except (FileNotFoundError, ProcessLookupError):
        return False


def test_result_document_and_transcript_match_what_upstream_reads(tmp_path: Path) -> None:
    """Emit a Claude-shaped result and a per-project transcript that ``collect_usage`` can price.

    Args:
        tmp_path: Pytest's temporary folder.
    """
    outcome = SessionOutcome(
        text="answer",
        stdout="",
        stderr="",
        returncode=0,
        usage={"input_tokens": 7, "output_tokens": 3},
        cost_usd=0.5,
        duration_seconds=1.5,
    )
    document = result_document("sid", "m1", outcome)
    assert document["type"] == "result"
    assert document["is_error"] is False
    assert document["result"] == "answer"
    assert document["session_id"] == "sid"
    assert document["total_cost_usd"] == 0.5
    assert document["modelUsage"]["m1"]["inputTokens"] == 7
    assert document["modelUsage"]["m1"]["outputTokens"] == 3
    home = tmp_path / "home"
    os.environ["HOME"] = str(home)
    try:
        record_transcript("sid", tmp_path / "work", "m1", outcome.usage)
    finally:
        os.environ["HOME"] = str(Path.home())
    transcripts = list((home / ".claude" / "projects").rglob("sid.jsonl"))
    assert len(transcripts) == 1
    line = json.loads(transcripts[0].read_text().splitlines()[0])
    assert line["message"]["model"] == "m1"
    assert line["message"]["usage"]["input_tokens"] == 7
    assert line["message"]["usage"]["output_tokens"] == 3


def test_shim_resumes_by_restating_the_opening_prompt(tmp_path: Path) -> None:
    """Replay the first task before the follow-up nudge because other harnesses cannot resume a session id.

    Args:
        tmp_path: Pytest's temporary folder.
    """
    home = tmp_path / "home"
    workspace = tmp_path / "work"
    workspace.mkdir()
    shim_dir = install_shim(home, Path(harness_bridge.__file__).resolve(), sys.executable)
    config = tmp_path / "proposer.json"
    config.write_text(json.dumps({**_PLAIN, "model": "m1"}))
    env = {
        **os.environ,
        "HOME": str(home),
        harness_bridge.CONFIG_ENV: str(config),
        "PATH": f"{shim_dir}{os.pathsep}{os.environ['PATH']}",
    }
    first = subprocess.run(
        ["claude", "--print", "first task", "--output-format", "json", "--session-id", "s1"],
        cwd=workspace,
        env=env,
        capture_output=True,
        text=True,
        check=True,
    )
    assert json.loads(first.stdout)["result"] == "first task"
    second = subprocess.run(
        ["claude", "-p", "--output-format", "json", "--resume", "s1", "keep going"],
        cwd=workspace,
        env=env,
        capture_output=True,
        text=True,
        check=True,
    )
    replayed = json.loads(second.stdout)["result"]
    assert replayed.startswith("first task")
    assert replayed.endswith("keep going")
    assert (home / harness_bridge.SESSIONS_DIR / "s1" / "prompt-2.md").exists()
    rejected = subprocess.run(
        ["claude", "--print", "x", "--output-format", "text"],
        cwd=workspace,
        env=env,
        capture_output=True,
        text=True,
        check=False,
    )
    assert rejected.returncode == 1
    assert "unsupported --output-format" in rejected.stderr


def test_failed_result_names_the_error_event_codex_writes_to_stdout() -> None:
    """Keep Codex's stdout error events next to its stderr banner, which says nothing on its own."""
    stdout = "\n".join(
        [
            json.dumps({"type": "thread.started", "thread_id": "t1"}),
            json.dumps({"type": "turn.started"}),
            json.dumps({"type": "error", "message": "unexpected status 401 Unauthorized"}),
            json.dumps({"type": "turn.failed", "error": {"message": "stream disconnected before completion"}}),
        ]
    )
    outcome = SessionOutcome(
        text=None,
        stdout=stdout,
        stderr="Reading additional input from stdin...\n",
        returncode=1,
        usage={},
        cost_usd=None,
        duration_seconds=2.0,
    )
    document = result_document("sid", "m1", outcome)
    assert document["is_error"] is True
    assert "Reading additional input from stdin..." in document["result"]
    assert "unexpected status 401 Unauthorized" in document["result"]
    assert "stream disconnected before completion" in document["result"]
    assert "thread.started" not in document["result"]


def test_failed_result_falls_back_to_plain_stdout_lines() -> None:
    """Report the tail of non-JSON stdout when the harness wrote no error events."""
    outcome = SessionOutcome(
        text=None,
        stdout="booting\nfatal: config missing\n",
        stderr="",
        returncode=2,
        usage={},
        cost_usd=None,
        duration_seconds=0.1,
    )
    assert harness_bridge.failure_detail(outcome) == "stdout: booting\nfatal: config missing"


def test_harness_gets_an_empty_stdin(tmp_path: Path) -> None:
    """Give the harness a closed stdin so a CLI that reads it never waits on the bridge's own input.

    Args:
        tmp_path: Pytest's temporary folder.
    """
    proposer = {**_PLAIN, "run_command": 'if read -r line; then echo "got:$line"; else echo eof; fi'}
    workspace = tmp_path / "w"
    workspace.mkdir()
    outcome = run_session(
        proposer, workspace=workspace, prompt="x", model="m1", session_dir=tmp_path / "s", timeout_seconds=10
    )
    assert outcome.stdout.strip() == "eof"


def test_shim_reports_why_the_harness_failed_on_stderr(tmp_path: Path) -> None:
    """Surface the harness's own error even when its stdout ends with an ordinary chat message.

    Args:
        tmp_path: Pytest's temporary folder.
    """
    home = tmp_path / "home"
    workspace = tmp_path / "work"
    workspace.mkdir()
    shim_dir = install_shim(home, Path(harness_bridge.__file__).resolve(), sys.executable)
    config = tmp_path / "proposer.json"
    failing = {**_PLAIN, "run_command": "echo 'I will now edit the scorer.'; echo 'stream disconnected' >&2; exit 3"}
    config.write_text(json.dumps({**failing, "model": "m1"}))
    env = {
        **os.environ,
        "HOME": str(home),
        harness_bridge.CONFIG_ENV: str(config),
        "PATH": f"{shim_dir}{os.pathsep}{os.environ['PATH']}",
    }
    failed = subprocess.run(
        ["claude", "--print", "task", "--output-format", "json"],
        cwd=workspace,
        env=env,
        capture_output=True,
        text=True,
        check=False,
    )
    assert failed.returncode == 3
    assert json.loads(failed.stdout)["is_error"] is True
    assert "stream disconnected" in failed.stderr


def _tool_events_command(count: int) -> str:
    """Build a shell command that edits a file, streams Pi tool-call events, then lingers.

    Args:
        count: Tool calls to announce.

    Returns:
        The command.
    """
    lines = " ".join(
        f"echo '{json.dumps({'type': 'tool_execution_start', 'toolCallId': f'c{index}'})}';" for index in range(count)
    )
    return f"echo edited > note.txt; {lines} sleep 5; echo late > late.txt"


def test_tool_call_cap_stops_the_harness_and_keeps_its_edits(tmp_path: Path) -> None:
    """Stop a harness once it starts more tool calls than the cap, without calling the session failed.

    Args:
        tmp_path: Pytest's temporary folder.
    """
    proposer = {**_PLAIN, "output_format": "pi", "run_command": _tool_events_command(5), "max_tool_calls": 3}
    workspace = tmp_path / "work"
    outcome = run_session(proposer, workspace=workspace, prompt="x", model="m", session_dir=tmp_path / "session")
    assert outcome.tool_cap_reached is True
    # Events already in the pipe when the stop lands are still counted.
    assert outcome.tool_calls > 3
    assert outcome.duration_seconds < 4
    assert outcome.failed is False
    assert (workspace / "note.txt").read_text() == "edited\n"
    assert not (workspace / "late.txt").exists()
    document = result_document("sid", "m", outcome)
    assert document["is_error"] is False
    assert "tool-call cap" in document["result"]


def test_tool_calls_are_counted_but_not_capped_without_a_cap(tmp_path: Path) -> None:
    """Let a harness run to its end when the launch names no cap.

    Args:
        tmp_path: Pytest's temporary folder.
    """
    command = _tool_events_command(5).replace("sleep 5; ", "")
    proposer = {**_PLAIN, "output_format": "pi", "run_command": command}
    outcome = run_session(proposer, workspace=tmp_path / "w", prompt="x", model="m", session_dir=tmp_path / "s")
    assert outcome.tool_cap_reached is False
    assert outcome.returncode == 0
    assert (tmp_path / "w" / "late.txt").exists()


def test_tool_call_ids_read_every_streaming_format() -> None:
    """Name tool calls in the Pi, Codex and OpenCode stream shapes, and none in plain output."""
    tool_call_ids = harness_bridge.tool_call_ids
    assert tool_call_ids("pi", {"type": "tool_execution_start", "toolCallId": "a"}) == ["a"]
    assert tool_call_ids("pi", {"type": "tool_execution_end", "toolCallId": "a"}) == []
    started = {"type": "item.started", "item": {"id": "i1", "type": "command_execution"}}
    assert tool_call_ids("codex", started) == ["i1"]
    assert tool_call_ids("codex", {**started, "type": "item.completed"}) == ["i1"]
    assert tool_call_ids("codex", {"type": "item.completed", "item": {"id": "i2", "type": "agent_message"}}) == []
    assert tool_call_ids("opencode", {"type": "tool_use", "part": {"callID": "o1"}}) == ["o1"]
    assert tool_call_ids("plain", {"type": "tool_execution_start"}) == []


def test_codex_tool_call_counts_once_across_start_and_completion(tmp_path: Path) -> None:
    """Count a Codex item announced on start and settled on completion as one call.

    Args:
        tmp_path: Pytest's temporary folder.
    """
    item = {"id": "i1", "type": "command_execution"}
    events = [{"type": "item.started", "item": item}, {"type": "item.completed", "item": item}]
    command = "; ".join(f"echo '{json.dumps(event)}'" for event in events)
    proposer = {**_PLAIN, "output_format": "codex", "run_command": command, "max_tool_calls": 5}
    outcome = run_session(proposer, workspace=tmp_path / "w", prompt="x", model="m", session_dir=tmp_path / "s")
    assert outcome.tool_calls == 1
    assert outcome.tool_cap_reached is False


# The worker's hidden list lands with SKY-386 and SKY-387; these tests use a fixed one.
_HIDDEN = [
    "ascent.toml",
    "metric",
    ".optimize/exploits",
    "reference/answers.json",
    "data/cases.jsonl",
    "rules/illegal.md",
    "tools/mcp_server.py",
]
_HIDDEN_FILES = ["ascent.toml", "metric/score.py", ".optimize/exploits/log.md", *_HIDDEN[3:]]
# One shape per harness tool family: claude, codex, pi, opencode, prime and their shells.
_TOOL_CALLS = [
    ("Read", lambda path: {"file_path": path}),
    ("Grep", lambda path: {"pattern": "def", "path": path}),
    ("Glob", lambda path: {"pattern": path}),
    ("LS", lambda path: {"path": path}),
    ("Bash", lambda path: {"command": f"cat {path}"}),
    ("exec_command", lambda path: {"cmd": f"bash -lc 'head -n 5 {path}'"}),
    ("read", lambda path: {"path": path}),
    ("ls", lambda path: {"path": path}),
    ("grep", lambda path: {"pattern": "x", "path": path}),
    ("find", lambda path: {"pattern": "*", "path": path}),
    ("bash", lambda path: {"command": f"python -c \"print(open('{path}').read())\""}),
    ("list", lambda path: {"dirPath": path}),
    ("apply_patch", lambda path: {"input": f"*** Begin Patch\n*** Update File: {path}\n*** End Patch"}),
    ("ipython", lambda path: {"code": f'print(open("{path}").read())'}),
]


def _policy(tmp_path: Path) -> dict:
    """Build a guard policy over the fixed hidden list.

    Args:
        tmp_path: Pytest's temporary folder.

    Returns:
        The guard policy.
    """
    workspace = tmp_path / "work"
    workspace.mkdir(exist_ok=True)
    return harness_bridge.guard_policy(workspace, _HIDDEN, tmp_path / "refusals.jsonl")


@pytest.mark.parametrize("path", _HIDDEN_FILES)
@pytest.mark.parametrize(("tool", "shape"), _TOOL_CALLS, ids=[tool for tool, _ in _TOOL_CALLS])
def test_guard_refuses_every_hidden_path_by_exact_name_in_every_tool(tmp_path: Path, tool, shape, path) -> None:
    """Refuse a read, grep, glob, list or shell call on each hidden path, with the right kind.

    Args:
        tmp_path: Pytest's temporary folder.
        tool: The tool name.
        shape: Builds the tool input for a path.
        path: The hidden path.
    """
    policy = _policy(tmp_path)
    expected = "config" if path == "ascent.toml" else "scoring"
    assert harness_bridge.guard_check(policy, tool, shape(path)) == expected
    absolute = f"{policy['workspace']}/{path}"
    assert harness_bridge.guard_check(policy, tool, shape(absolute)) == expected


@pytest.mark.parametrize(
    ("tool", "tool_input"),
    [
        ("Glob", {"pattern": "**/*"}),
        ("Bash", {"command": "ls -R ."}),
        ("Grep", {"pattern": "ascent.toml", "path": "src"}),
        ("Read", {"file_path": "src/ascent.toml.md"}),
        ("Read", {"file_path": "metrics/score.py"}),
        ("Read", {"file_path": "../work-other/ascent.toml"}),
    ],
)
def test_guard_lets_other_paths_and_broad_patterns_run(tmp_path: Path, tool: str, tool_input: dict) -> None:
    """Match exact hidden names only, so broad globs and look-alike names still work.

    Args:
        tmp_path: Pytest's temporary folder.
        tool: The tool name.
        tool_input: A tool input that names no hidden path.
    """
    assert harness_bridge.guard_check(_policy(tmp_path), tool, tool_input) is None


def test_guard_policy_holds_no_hidden_names(tmp_path: Path) -> None:
    """Keep the hidden names out of the policy file the agent's workspace can read.

    Args:
        tmp_path: Pytest's temporary folder.
    """
    text = json.dumps(_policy(tmp_path))
    for path in _HIDDEN:
        assert path not in text


@pytest.mark.parametrize(
    ("tool", "tool_input"),
    [
        ("Read", {"file_path": "alias/ascent.toml"}),
        ("Bash", {"command": "cd src && cat ../ascent.toml"}),
        ("Bash", {"command": "cd src; cd deep && cat ../../metric/score.py"}),
        ("exec_command", {"cmd": "cat ../ascent.toml", "workdir": "src"}),
        ("Bash", {"command": 'cat "$(pwd)/ascent.toml"'}),
        ("Bash", {"command": "cat $(pwd)/ascent.toml"}),
        ("Bash", {"command": "cat `pwd`/data/cases.jsonl"}),
        ("Read", {"file_path": "${PWD}/ascent.toml"}),
    ],
    ids=[
        "symlink-alias",
        "cd-parent",
        "cd-chain",
        "codex-workdir",
        "quoted-pwd",
        "bare-pwd",
        "backquote-pwd",
        "brace-pwd",
    ],
)
def test_guard_resolves_aliases_working_directories_and_pwd(tmp_path: Path, tool: str, tool_input: dict) -> None:
    """Refuse a hidden path that a symbolic link, a cd, a workdir or a pwd form names.

    Args:
        tmp_path: Pytest's temporary folder.
        tool: The tool name.
        tool_input: A tool input that names a hidden path through a link, a cd, a workdir or a pwd.
    """
    policy = _policy(tmp_path)
    (Path(policy["workspace"]) / "alias").symlink_to(".")
    assert harness_bridge.guard_check(policy, tool, tool_input) is not None


def test_guard_folds_case_only_when_the_policy_says_so(tmp_path: Path) -> None:
    """Refuse ASCENT.TOML on a case-folding policy, and only there.

    Args:
        tmp_path: Pytest's temporary folder.
    """
    workspace = tmp_path / "work"
    call = {"file_path": "ASCENT.TOML"}
    folded = harness_bridge.guard_policy(workspace, _HIDDEN, tmp_path / "log", fold_case=True)
    exact = harness_bridge.guard_policy(workspace, _HIDDEN, tmp_path / "log")
    assert harness_bridge.guard_check(folded, "Read", call) == "config"
    assert harness_bridge.guard_check(folded, "Read", {"file_path": "Metric/Score.py"}) == "scoring"
    assert harness_bridge.guard_check(exact, "Read", call) is None


@pytest.mark.parametrize(
    ("tool", "tool_input"),
    [
        ("Bash", {"command": "cat ~/shinka-run/repo/ascent.toml"}),
        ("Bash", {"command": "cat $HOME/shinka-run/repo/ascent.toml"}),
        ("Bash", {"command": "cat ${HOME}/shinka-run/repo/metric/score.py"}),
        ("Bash", {"command": "cd ~/shinka-run/repo && cat ascent.toml"}),
        ("Bash", {"command": "cd ~ && cat shinka-run/repo/data/cases.jsonl"}),
        ("Read", {"file_path": "~/shinka-run/repo/ascent.toml"}),
        ("exec_command", {"cmd": "cat ascent.toml", "workdir": "~/shinka-run/repo"}),
    ],
    ids=["tilde", "home", "brace-home", "cd-tilde", "cd-bare-tilde", "read-tilde", "codex-workdir-tilde"],
)
def test_guard_expands_the_home_folder_forms(tmp_path: Path, tool: str, tool_input: dict) -> None:
    """Refuse a hidden path that ``~``, ``$HOME`` or ``${HOME}`` names, as in a ShinkaEvolve session.

    Args:
        tmp_path: Pytest's temporary folder.
        tool: The tool name.
        tool_input: A tool input that names a hidden path through a home folder form.
    """
    home = tmp_path / "home"
    workspace = home / "shinka-run" / "repo"
    workspace.mkdir(parents=True)
    policy = harness_bridge.guard_policy(workspace, _HIDDEN, tmp_path / "log", home=home)
    assert policy["home"] == str(home)
    assert harness_bridge.guard_check(policy, tool, tool_input) is not None


@pytest.mark.parametrize(
    "command",
    ["cat x~/shinka-run/repo/ascent.toml", "cat ~/ascent.toml"],
)
def test_guard_expands_a_tilde_only_at_the_start_of_a_word(tmp_path: Path, command: str) -> None:
    """Leave a tilde inside a word, and a home path outside the workspace, alone.

    Args:
        tmp_path: Pytest's temporary folder.
        command: A shell command whose tilde must stay as it is.
    """
    home = tmp_path / "home"
    workspace = home / "shinka-run" / "repo"
    workspace.mkdir(parents=True)
    policy = harness_bridge.guard_policy(workspace, _HIDDEN, tmp_path / "log", home=home)
    assert harness_bridge.guard_check(policy, "Bash", {"command": command}) is None


@pytest.mark.parametrize(
    "command",
    [
        "cat ~+/ascent.toml",
        "cd metric && cat ~-/ascent.toml",
        "cat $(pwd -P)/ascent.toml",
        "cat $(pwd -L)/ascent.toml",
        "cat ${PWD:-x}/ascent.toml",
        "cat ${HOME:-x}/shinka-run/repo/ascent.toml",
        "cat ${HOME%/}/shinka-run/repo/ascent.toml",
        "cd metric; cat $OLDPWD/ascent.toml",
        "cat ~agent/shinka-run/repo/ascent.toml",
        "cat $HOMEDIR/shinka-run/repo/ascent.toml",
        "cat `pwd -P`/metric/score.py",
        'cat "$(dirname x)"/data/cases.jsonl',
    ],
)
def test_guard_refuses_a_hidden_name_after_an_unresolved_expansion(tmp_path: Path, command: str) -> None:
    """Refuse a hidden name after any shell expansion the guard cannot resolve to a known folder.

    Args:
        tmp_path: Pytest's temporary folder.
        command: A shell command with a hidden name after an expansion the guard cannot resolve.
    """
    home = tmp_path / "home"
    workspace = home / "shinka-run" / "repo"
    workspace.mkdir(parents=True)
    policy = harness_bridge.guard_policy(workspace, _HIDDEN, tmp_path / "log", home=home)
    assert harness_bridge.guard_check(policy, "Bash", {"command": command}) is not None


@pytest.mark.parametrize(
    "command",
    [
        "cd src && python $(pwd -P)/solve.py --out ${OUT:-out}/result.json",
        "echo $PATH ~+ ~- `date` && cat src/ascent.toml.md",
        "cat ${HOME:-x}/notes/metrics.md",
    ],
)
def test_guard_lets_an_expansion_without_a_hidden_name_run(tmp_path: Path, command: str) -> None:
    """Let a normal command run when no hidden name follows its shell expansions.

    Args:
        tmp_path: Pytest's temporary folder.
        command: A shell command with expansions and no hidden name.
    """
    home = tmp_path / "home"
    workspace = home / "shinka-run" / "repo"
    workspace.mkdir(parents=True)
    policy = harness_bridge.guard_policy(workspace, _HIDDEN, tmp_path / "log", home=home)
    assert harness_bridge.guard_check(policy, "Bash", {"command": command}) is None


def test_guard_policy_home_defaults_to_this_process_home(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    """Record this process's home folder when the caller names none, so the digest covers it.

    Args:
        tmp_path: Pytest's temporary folder.
        monkeypatch: Pytest monkeypatch.
    """
    monkeypatch.setenv("HOME", str(tmp_path / "agent-home"))
    assert _policy(tmp_path)["home"] == str(tmp_path / "agent-home")


def test_guard_folds_the_case_of_the_workspace_root(tmp_path: Path) -> None:
    """Refuse an absolute hidden path whose root folder has another case, on a case-folding policy.

    Args:
        tmp_path: Pytest's temporary folder.
    """
    workspace = tmp_path / "work"
    workspace.mkdir()
    folded = harness_bridge.guard_policy(workspace, _HIDDEN, tmp_path / "log", fold_case=True)
    first, rest = str(workspace).lstrip("/").split("/", 1)
    call = {"file_path": f"/{first.upper()}/{rest}/ascent.toml"}
    assert harness_bridge.guard_check(folded, "Read", call) == "config"
    assert harness_bridge.guard_check(_policy(tmp_path), "Read", call) is None


def _digest(policy_file: Path) -> str:
    """Hash the policy file as the guard checks it.

    Args:
        policy_file: The guard policy file.

    Returns:
        The SHA-256 hex digest of the file.
    """
    return hashlib.sha256(policy_file.read_bytes()).hexdigest()


def _guard(mode: str, policy_file: Path, request: dict) -> dict:
    """Run the guard CLI once and parse its answer.

    Args:
        mode: The guard CLI mode, ``hook`` or ``check``.
        policy_file: The guard policy file.
        request: The tool call to send to the guard.

    Returns:
        The guard's parsed answer.
    """
    completed = subprocess.run(
        harness_bridge.guard_command(mode, policy_file, _digest(policy_file)),
        input=json.dumps(request),
        capture_output=True,
        text=True,
        check=True,
    )
    return json.loads(completed.stdout or "{}")


def test_guard_cli_answers_codex_hooks_and_js_checks_and_logs_only_tool_and_kind(tmp_path: Path) -> None:
    """Deny in the Codex hook shape, answer the JS check shape, and log no path or content.

    Args:
        tmp_path: Pytest's temporary folder.
    """
    session = tmp_path / "session"
    env = harness_bridge.install_guard(session, tmp_path / "work", harness_bridge.hidden_hashes(_HIDDEN))
    policy_file = session / harness_bridge.GUARD_DIR / "policy.json"
    denied = _guard("hook", policy_file, {"tool_name": "exec_command", "tool_input": {"cmd": "cat ascent.toml"}})
    assert denied["hookSpecificOutput"]["permissionDecision"] == "deny"
    assert denied["hookSpecificOutput"]["permissionDecisionReason"] == harness_bridge.GUARD_REFUSAL
    assert _guard("hook", policy_file, {"tool_name": "exec_command", "tool_input": {"cmd": "ls src"}}) == {}
    checked = _guard("check", policy_file, {"tool": "read", "input": {"path": "data/cases.jsonl"}})
    assert checked == {"reason": harness_bridge.GUARD_REFUSAL}
    assert _guard("check", policy_file, {"tool": "read", "input": {"path": "src/app.py"}}) == {"reason": None}
    assert harness_bridge.read_refusals(session) == [
        {"tool": "exec_command", "kind": "config"},
        {"tool": "read", "kind": "scoring"},
    ]
    hooks = json.loads(Path(env["APP_GUARD_CODEX"]).read_text())
    assert "guard hook" in hooks["hooks"]["PreToolUse"][0]["hooks"][0]["command"]
    assert Path(env["APP_GUARD_PI"]).exists()
    assert (Path(env["OPENCODE_CONFIG_DIR"]) / "plugin" / "app-guard.js").exists()
    assert Path(env["APP_GUARD_EVE"]).name == "eve-guard.mjs"


def test_read_refusals_skips_lines_the_agent_broke(tmp_path: Path) -> None:
    """Skip a line that is not JSON, too deep to parse, a non-object, or without a string tool and kind.

    Args:
        tmp_path: Pytest's temporary folder.
    """
    log = tmp_path / harness_bridge.GUARD_DIR / "refusals.jsonl"
    log.parent.mkdir()
    lines = [
        json.dumps({"tool": "Bash", "kind": "scoring", "path": "metric.py"}),
        "not json {",
        "5",
        json.dumps(["Read", "config"]),
        json.dumps({"tool": "Read"}),
        json.dumps({"tool": 3, "kind": "config"}),
        "[" * 5000,
        json.dumps({"tool": "Read", "kind": "config"}),
    ]
    log.write_text("\n".join(lines) + "\n", encoding="utf-8")

    assert harness_bridge.read_refusals(tmp_path) == [
        {"tool": "Bash", "kind": "scoring"},
        {"tool": "Read", "kind": "config"},
    ]


@pytest.mark.parametrize(
    "command",
    [
        "cd . && cd . && cd . && cd . && cd data && cat cases.jsonl",
        "cd {work} && cd {work} && cd {work} && cd {work} && cd {work}/data && cat cases.jsonl",
        "pushd . && pushd . && pushd . && pushd . && pushd data && cat cases.jsonl",
    ],
    ids=["five-cd", "absolute-cd", "pushd"],
)
def test_guard_refuses_more_directory_changes_than_it_can_follow(tmp_path: Path, command: str) -> None:
    """Refuse a call with more cd or pushd steps than the guard follows, in the check and the CLI.

    Args:
        tmp_path: Pytest's temporary folder.
        command: A shell command with more folder changes than the guard follows.
    """
    session = tmp_path / "session"
    harness_bridge.install_guard(session, tmp_path / "work", harness_bridge.hidden_hashes(_HIDDEN))
    policy_file = session / harness_bridge.GUARD_DIR / "policy.json"
    command = command.format(work=tmp_path / "work")
    with pytest.raises(ValueError, match="directory changes"):
        harness_bridge.guard_check(json.loads(policy_file.read_text()), "Bash", {"command": command})
    denied = _guard("hook", policy_file, {"tool_name": "Bash", "tool_input": {"command": command}})
    assert denied["hookSpecificOutput"]["permissionDecision"] == "deny"


@pytest.mark.parametrize(
    "command",
    [
        "cd -- data && cat cases.jsonl",
        "cd -P data && cat cases.jsonl",
        "cd -L data && cat cases.jsonl",
        "pushd -- data && cat cases.jsonl",
        "env -C data cat cases.jsonl",
        "env --chdir=data cat cases.jsonl",
        "env -Cdata cat cases.jsonl",
        "git -C data show HEAD:cases.jsonl",
    ],
)
def test_guard_follows_folder_options(tmp_path: Path, command: str) -> None:
    """Refuse a hidden name behind a ``cd`` option or a folder option such as ``env -C``.

    Args:
        tmp_path: Pytest's temporary folder.
        command: A shell command that reaches a hidden name through a folder option.
    """
    assert harness_bridge.guard_check(_policy(tmp_path), "Bash", {"command": command}) == "scoring"


@pytest.mark.parametrize(
    "command",
    [
        "grep -C 3 foo notes.txt",
        "cd - && cat notes.txt",
        "cd -P src && cat notes.txt",
        "git -C a status && git -C b status && git -C c status",
        "git -C sub status && git -C sub diff && git -C sub log",
        " && ".join(["grep -C 3 foo f.py"] * 5),
    ],
)
def test_guard_allows_ordinary_option_forms(tmp_path: Path, command: str) -> None:
    """Allow a context count after ``grep -C`` and a ``cd`` option before an ordinary folder.

    Args:
        tmp_path: Pytest's temporary folder.
        command: A shell command with ordinary option forms.
    """
    assert harness_bridge.guard_check(_policy(tmp_path), "Bash", {"command": command}) is None


@pytest.mark.parametrize("command", ["cd; cat shinka-run/repo/ascent.toml", "cd && cat shinka-run/repo/ascent.toml"])
def test_guard_follows_a_bare_cd_to_the_home_folder(tmp_path: Path, command: str) -> None:
    """Refuse a hidden path after a ``cd`` with no folder, which goes to the home folder.

    Args:
        tmp_path: Pytest's temporary folder.
        command: A shell command with a bare ``cd`` before a hidden path.
    """
    home = tmp_path / "home"
    workspace = home / "shinka-run" / "repo"
    workspace.mkdir(parents=True)
    policy = harness_bridge.guard_policy(workspace, _HIDDEN, tmp_path / "log", home=home)
    assert harness_bridge.guard_check(policy, "Bash", {"command": command}) == "config"
    assert harness_bridge.guard_check(policy, "Bash", {"command": "cd; cat notes/optimize.md"}) is None


def test_guard_checks_a_call_in_the_folder_the_hook_event_names(tmp_path: Path) -> None:
    """Refuse a hidden name relative to the event's ``cwd``, as after an earlier ``cd data`` call.

    Args:
        tmp_path: Pytest's temporary folder.
    """
    session = tmp_path / "session"
    harness_bridge.install_guard(session, tmp_path / "work", harness_bridge.hidden_hashes(_HIDDEN))
    policy_file = session / harness_bridge.GUARD_DIR / "policy.json"
    policy = json.loads(policy_file.read_text())
    call = {"command": "cat cases.jsonl"}
    assert harness_bridge.guard_check(policy, "Bash", call) is None
    assert harness_bridge.guard_check(policy, "Bash", call, "data") == "scoring"
    assert harness_bridge.guard_check(policy, "Bash", call, str(tmp_path / "work" / "data")) == "scoring"
    assert harness_bridge.guard_check(policy, "Bash", {"command": "cat score.py"}, "data") is None
    event = {"tool_name": "Bash", "tool_input": call, "cwd": str(tmp_path / "work" / "data")}
    assert _guard("hook", policy_file, event)["hookSpecificOutput"]["permissionDecision"] == "deny"


def _link_tree(workspace: Path, max_target: int) -> list[str]:
    """Make about 2,000 links whose targets are long runs of parts that are not links.

    Each top link points at mid links, and each mid link at leaf links, so one
    candidate makes the resolver walk every target. A cap on links alone let
    this call run for 30 s.

    Args:
        workspace: The folder that gets the links.
        max_target: The longest link target the system allows.

    Returns:
        The names of the top links.
    """

    def filler(tag: str, budget: int) -> str:
        """Return a target of ``budget`` characters that ends where it starts.

        Args:
            tag: The first part of the target.
            budget: The length of the target.

        Returns:
            The link target.
        """
        count = max(0, (budget - len(tag) - 4) // 5)
        return tag + "/" + "a/" * count + "../" * (count + 1)

    leaves = []
    for mid in range(44):
        names = []
        for leaf in range(44):
            name = f"L{mid}_{leaf}"
            (workspace / name).symlink_to(filler(f"q{mid}_{leaf}", max_target))
            names.append(name)
        target = "/".join(names) + "/"
        (workspace / f"M{mid}").symlink_to(target + filler(f"r{mid}", max_target - len(target)))
        leaves.append(f"M{mid}")
    target = "/".join(leaves) + "/"
    (workspace / "T0").symlink_to(target + filler("s0", max_target - len(target)))
    return ["T0"]


def test_guard_denies_a_link_tree_of_long_targets_inside_the_hook_timeout(tmp_path: Path) -> None:
    """Deny a call whose links point at long paths, well before the 10 s hook timeout.

    Args:
        tmp_path: Pytest's temporary folder.
    """
    session = tmp_path / "session"
    workspace = tmp_path / "work"
    workspace.mkdir()
    harness_bridge.install_guard(session, workspace, harness_bridge.hidden_hashes(_HIDDEN))
    policy_file = session / harness_bridge.GUARD_DIR / "policy.json"
    max_target = 1023 if sys.platform == "darwin" else 4095
    command = "cat " + " ".join(_link_tree(workspace, max_target))
    started = time.monotonic()
    denied = _guard("hook", policy_file, {"tool_name": "Bash", "tool_input": {"command": command}})
    assert denied["hookSpecificOutput"]["permissionDecision"] == "deny"
    # The guard stops at its 5 s deadline or its step cap; CLI start-up adds the rest.
    assert time.monotonic() - started < 8


def test_guard_refuses_long_symbolic_link_chains_fast(tmp_path: Path) -> None:
    """Refuse a call whose candidates go through a long link chain, before the hook timeout.

    ``os.path.realpath`` follows the whole chain for each candidate, so this
    call kept the guard busy for more than 15 s before the link cap.

    Args:
        tmp_path: Pytest's temporary folder.
    """
    session = tmp_path / "session"
    harness_bridge.install_guard(session, tmp_path / "work", harness_bridge.hidden_hashes(_HIDDEN))
    policy_file = session / harness_bridge.GUARD_DIR / "policy.json"
    workspace = tmp_path / "work"
    workspace.mkdir(exist_ok=True)
    for index in range(900):
        (workspace / f"c{index}").symlink_to(f"c{index + 1}")
    for index in range(3000):
        (workspace / f"e{index}").symlink_to("c0")
    command = "cat " + " ".join(f"e{index}" for index in range(3000)) + " ascent.toml"
    with pytest.raises(ValueError, match="symbolic links"):
        harness_bridge.guard_check(json.loads(policy_file.read_text()), "Bash", {"command": command})
    started = time.monotonic()
    denied = _guard("hook", policy_file, {"tool_name": "Bash", "tool_input": {"command": command}})
    assert denied["hookSpecificOutput"]["permissionDecision"] == "deny"
    assert time.monotonic() - started < 3


def test_guard_refuses_a_symbolic_link_loop(tmp_path: Path) -> None:
    """Refuse a path behind a link loop, which the system cannot open either.

    Args:
        tmp_path: Pytest's temporary folder.
    """
    policy = _policy(tmp_path)
    workspace = tmp_path / "work"
    (workspace / "loop1").symlink_to("loop2")
    (workspace / "loop2").symlink_to("loop1")
    with pytest.raises(ValueError, match="symbolic links"):
        harness_bridge.guard_check(policy, "Bash", {"command": "cat loop1"})


def test_guard_follows_a_short_link_chain_to_a_hidden_file(tmp_path: Path) -> None:
    """Resolve a hidden file behind a chain of links, as ``os.path.realpath`` does.

    Args:
        tmp_path: Pytest's temporary folder.
    """
    policy = _policy(tmp_path)
    workspace = tmp_path / "work"
    (workspace / "link2").symlink_to("ascent.toml")
    (workspace / "sub").mkdir()
    (workspace / "sub" / "link1").symlink_to("../link2")
    (workspace / "link0").symlink_to("sub/link1")
    assert harness_bridge.guard_check(policy, "Read", {"file_path": "link0"}) == "config"


def test_guard_still_follows_four_directory_changes(tmp_path: Path) -> None:
    """Resolve a hidden path behind the largest number of cd steps the guard follows.

    Args:
        tmp_path: Pytest's temporary folder.
    """
    command = "cd . && cd . && cd . && cd data && cat cases.jsonl"
    assert harness_bridge.guard_check(_policy(tmp_path), "Bash", {"command": command}) == "scoring"


@pytest.mark.parametrize(
    "command",
    [
        "echo " + "w " * 40_000,
        "cd a && cd b && cd c && " + " ".join(f"w{i}" for i in range(2_000)),
        "cat $X/" + "/".join(["a"] * 40),
    ],
    ids=["too-long", "too-many-checks", "too-deep-after-expansion"],
)
def test_guard_refuses_a_call_above_its_cost_caps_fast(tmp_path: Path, command: str) -> None:
    """Refuse a call the guard cannot check well inside the hook timeout, and answer fast.

    Args:
        tmp_path: Pytest's temporary folder.
        command: A shell command above one of the guard's cost caps.
    """
    session = tmp_path / "session"
    harness_bridge.install_guard(session, tmp_path / "work", harness_bridge.hidden_hashes(_HIDDEN))
    policy_file = session / harness_bridge.GUARD_DIR / "policy.json"
    with pytest.raises(ValueError):
        harness_bridge.guard_check(json.loads(policy_file.read_text()), "Bash", {"command": command})
    started = time.monotonic()
    denied = _guard("hook", policy_file, {"tool_name": "Bash", "tool_input": {"command": command}})
    assert denied["hookSpecificOutput"]["permissionDecision"] == "deny"
    # The hook timeout is 10 s; the CLI start-up is most of this time.
    assert time.monotonic() - started < 3


_NODE = shutil.which("node")


@pytest.mark.skipif(_NODE is None, reason="node is not installed")
def test_pi_extension_and_opencode_plugin_refuse_hidden_paths(tmp_path: Path) -> None:
    """Block a hidden read in the Pi extension, the OpenCode plugin and the eve module; let others run.

    Args:
        tmp_path: Pytest's temporary folder.
    """
    session = tmp_path / "session"
    env = harness_bridge.install_guard(session, tmp_path / "work", harness_bridge.hidden_hashes(_HIDDEN))
    pi_module = tmp_path / "pi-guard.mjs"
    pi_module.write_text(Path(env["APP_GUARD_PI"]).read_text())
    plugin = Path(env["OPENCODE_CONFIG_DIR"]) / "plugin" / "app-guard.js"
    plugin_module = tmp_path / "app-guard.mjs"
    plugin_module.write_text(plugin.read_text())
    script = f"""
const {{ appGuardReason }} = await import({json.dumps(Path(env["APP_GUARD_EVE"]).as_uri())});
const eveRefused = appGuardReason("read_file", {{ path: "data/cases.jsonl" }});
const eveAllowed = appGuardReason("read_file", {{ path: "src/app.py" }});
const pi = (await import({json.dumps(pi_module.as_uri())})).default;
let handler;
pi({{ on: (name, fn) => {{ handler = fn; }} }});
const blocked = await handler({{ toolName: "read", input: {{ path: "metric/score.py" }} }});
const allowed = await handler({{ toolName: "read", input: {{ path: "src/app.py" }} }});
const {{ AppGuard }} = await import({json.dumps(plugin_module.as_uri())});
const hooks = await AppGuard({{}});
let thrown = null;
try {{ await hooks["tool.execute.before"]({{ tool: "read" }}, {{ args: {{ filePath: "ascent.toml" }} }}); }}
catch (error) {{ thrown = error.message; }}
await hooks["tool.execute.before"]({{ tool: "read" }}, {{ args: {{ filePath: "src/app.py" }} }});
console.log(JSON.stringify({{ blocked, allowed: allowed ?? null, thrown, eveRefused, eveAllowed }}));
"""
    completed = subprocess.run([_NODE, "--input-type=module", "-e", script], capture_output=True, text=True, check=True)
    result = json.loads(completed.stdout)
    assert result["blocked"] == {"block": True, "reason": harness_bridge.GUARD_REFUSAL}
    assert result["allowed"] is None
    assert result["thrown"] == harness_bridge.GUARD_REFUSAL
    assert result["eveRefused"] == harness_bridge.GUARD_REFUSAL
    assert result["eveAllowed"] is None


@pytest.mark.parametrize("mode", ["hook", "check"])
@pytest.mark.parametrize("broken", ["missing", "corrupt", "no-hashes", "emptied", "signed-empty", "surrogate"])
def test_guard_cli_refuses_when_it_cannot_answer(tmp_path: Path, mode: str, broken: str) -> None:
    """Refuse the call when the policy is gone, changed or broken, or the input cannot be hashed.

    Args:
        tmp_path: Pytest's temporary folder.
        mode: The guard CLI mode.
        broken: How the test breaks the policy or the input.
    """
    session = tmp_path / "session"
    harness_bridge.install_guard(session, tmp_path / "work", harness_bridge.hidden_hashes(_HIDDEN))
    policy_file = session / harness_bridge.GUARD_DIR / "policy.json"
    digest = _digest(policy_file)
    hook_input: dict = {"cmd": "cat src/app.py"}
    check_input: dict = {"path": "src/app.py"}
    if broken == "missing":
        policy_file.unlink()
    elif broken == "corrupt":
        policy_file.write_text("{not json")
    elif broken == "no-hashes":
        policy_file.write_text(json.dumps({"workspace": str(tmp_path / "work")}))
    elif broken == "emptied":
        # The agent can write the session folder and swap in a policy that allows everything.
        policy = json.loads(policy_file.read_text())
        policy_file.write_text(json.dumps({**policy, "hashes": {}}))
    elif broken == "signed-empty":
        policy = json.loads(policy_file.read_text())
        digest = harness_bridge.write_guard_policy(policy_file, {**policy, "hashes": {}})
    else:
        # The lone surrogate comes first, so a strict UTF-8 hash would crash before the hidden name.
        hook_input = {"cmd": "cat src/\ud800.py ascent.toml"}
        check_input = {"file_path": "ascent.toml", "path": "src/\ud800.py"}
    if mode == "hook":
        raw = json.dumps({"tool_name": "exec_command", "tool_input": hook_input})
    else:
        raw = json.dumps({"tool": "read", "input": check_input})
    completed = subprocess.run(
        harness_bridge.guard_command(mode, policy_file, digest), input=raw, capture_output=True, text=True, check=True
    )
    answer = json.loads(completed.stdout)
    if mode == "hook":
        assert answer["hookSpecificOutput"]["permissionDecision"] == "deny"
    else:
        assert answer == {"reason": harness_bridge.GUARD_REFUSAL}


@pytest.mark.skipif(_NODE is None, reason="node is not installed")
def test_pi_extension_refuses_when_the_guard_cannot_run(tmp_path: Path) -> None:
    """Block every call when the guard process fails, instead of letting it run.

    Args:
        tmp_path: Pytest's temporary folder.
    """
    session = tmp_path / "session"
    env = harness_bridge.install_guard(session, tmp_path / "work", harness_bridge.hidden_hashes(_HIDDEN))
    (session / harness_bridge.GUARD_DIR / "policy.json").unlink()
    broken = (
        Path(env["APP_GUARD_PI"]).read_text().replace(json.dumps(sys.executable), json.dumps("/nonexistent/python"))
    )
    pi_module = tmp_path / "pi-guard.mjs"
    pi_module.write_text(broken)
    script = f"""
const pi = (await import({json.dumps(pi_module.as_uri())})).default;
let handler;
pi({{ on: (name, fn) => {{ handler = fn; }} }});
console.log(JSON.stringify(await handler({{ toolName: "read", input: {{ path: "src/app.py" }} }})));
"""
    completed = subprocess.run([_NODE, "--input-type=module", "-e", script], capture_output=True, text=True, check=True)
    assert json.loads(completed.stdout) == {"block": True, "reason": harness_bridge.GUARD_REFUSAL}


def test_run_session_reports_refusals_and_keeps_running(tmp_path: Path) -> None:
    """Install the guard for a session with hidden paths, report each refusal, and finish the session.

    Args:
        tmp_path: Pytest's temporary folder.
    """
    command = (
        'printf \'{"tool":"read","input":{"path":".optimize/exploits"}}\' | '
        '"$APP_GUARD_PYTHON" "$APP_GUARD_BRIDGE" guard check "$APP_GUARD_POLICY" "$APP_GUARD_DIGEST"; echo done'
    )
    session = tmp_path / "session"
    guard_dir = session / harness_bridge.GUARD_DIR
    expected = harness_bridge.guard_policy(tmp_path / "work", _HIDDEN, guard_dir / "refusals.jsonl")
    digest = hashlib.sha256(json.dumps(expected).encode()).hexdigest()
    python, bridge, _, _, policy, _ = harness_bridge.guard_command("check", guard_dir / "policy.json", digest)
    proposer = {
        **_PLAIN,
        "run_command": command,
        "env": {
            "APP_GUARD_PYTHON": python,
            "APP_GUARD_BRIDGE": bridge,
            "APP_GUARD_POLICY": policy,
            "APP_GUARD_DIGEST": digest,
        },
        "hidden_hashes": harness_bridge.hidden_hashes(_HIDDEN),
    }
    outcome = run_session(proposer, workspace=tmp_path / "work", prompt="go", model="m1", session_dir=session)
    assert outcome.returncode == 0
    assert outcome.text.endswith("done")
    assert outcome.refusals == [{"tool": "read", "kind": "scoring"}]
    assert result_document("sid", "m1", outcome)["refusals"] == outcome.refusals
    record = harness_bridge.refusal_record(outcome.refusals[0])
    assert record["event"] == "proposer.refusal"
    assert record["fields"] == {"tool": "read", "kind": "scoring"}
    assert ".optimize" not in json.dumps(record)


def test_run_session_guards_the_agent_home_folder(tmp_path: Path) -> None:
    """Bind the guard to the HOME the agent runs with, so ``~`` resolves as in the agent's shell.

    Args:
        tmp_path: Pytest's temporary folder.
    """
    agent_home = tmp_path / "agent-home"
    session = tmp_path / "session"
    proposer = {
        **_PLAIN,
        "run_command": "echo done",
        "env": {"HOME": str(agent_home)},
        "hidden_hashes": harness_bridge.hidden_hashes(_HIDDEN),
    }
    run_session(proposer, workspace=tmp_path / "work", prompt="go", model="m1", session_dir=session)
    policy = json.loads((session / harness_bridge.GUARD_DIR / "policy.json").read_text())
    assert policy["home"] == str(agent_home)


def test_run_session_reports_host_agent_refusals(tmp_path: Path) -> None:
    """Take a host CLI turn's refusals from the shim's summary line, with no guard in the session.

    Args:
        tmp_path: Pytest's temporary folder.
    """
    line = json.dumps(
        {"type": "host_agent.result", "text": "ok", "usage": {}, "refusals": [{"tool": "Read", "kind": "config"}]}
    )
    proposer = {**_PLAIN, "output_format": "host_agent", "run_command": f"printf '%s\\n' '{line}'"}
    outcome = run_session(proposer, workspace=tmp_path / "work", prompt="go", model="m1", session_dir=tmp_path / "s")
    assert outcome.returncode == 0
    assert outcome.text == "ok"
    assert outcome.refusals == [{"tool": "Read", "kind": "config"}]


def test_run_session_without_hidden_paths_installs_no_guard(tmp_path: Path) -> None:
    """Leave sessions with no hidden list unchanged.

    Args:
        tmp_path: Pytest's temporary folder.
    """
    session = tmp_path / "session"
    outcome = run_session(_PLAIN, workspace=tmp_path / "work", prompt="go", model="m1", session_dir=session)
    assert outcome.refusals == []
    assert not (session / harness_bridge.GUARD_DIR).exists()


def test_codex_launch_installs_the_guard_hooks_before_it_starts(tmp_path: Path) -> None:
    """Copy the guard hooks into the session's Codex home only when the guard is set.

    Args:
        tmp_path: Pytest's temporary folder.
    """
    command = harness._codex_launch("m1", harness.GatewayConfig(url="https://gw.example/v1", api_key="k")).run_command
    assert command.startswith(harness._GUARD_CODEX_COPY)
    (tmp_path / ".app" / "codex").mkdir(parents=True)
    (tmp_path / "hooks.json").write_text("{}")
    subprocess.run(["bash", "-c", harness._GUARD_CODEX_COPY], cwd=tmp_path, env={}, check=True)
    assert not (tmp_path / ".app" / "codex" / "hooks.json").exists()
    env = {"APP_GUARD_CODEX": str(tmp_path / "hooks.json")}
    subprocess.run(["bash", "-c", harness._GUARD_CODEX_COPY], cwd=tmp_path, env=env, check=True)
    assert (tmp_path / ".app" / "codex" / "hooks.json").read_text() == "{}"


class _FakeResponses(http.server.BaseHTTPRequestHandler):
    """Answer the Responses API: one ``cat ascent.toml`` call, then a final message."""

    tool_outputs: list[str] = []

    def log_message(self, *args: object) -> None:
        """Keep the test output quiet.

        Args:
            *args: The log values, which the server ignores.
        """

    def do_POST(self) -> None:
        """Stream one model turn as server-sent events."""
        body = json.loads(self.rfile.read(int(self.headers["Content-Length"])))
        outputs = [item["output"] for item in body.get("input", []) if item.get("type") == "function_call_output"]
        _FakeResponses.tool_outputs.extend(outputs)
        if outputs:
            item = {
                "type": "message",
                "id": "m1",
                "role": "assistant",
                "content": [{"type": "output_text", "text": "done"}],
            }
        else:
            arguments = json.dumps({"cmd": "cat ascent.toml"})
            item = {
                "type": "function_call",
                "id": "f1",
                "call_id": "c1",
                "name": "exec_command",
                "arguments": arguments,
            }
        usage = {"input_tokens": 1, "output_tokens": 1, "total_tokens": 2}
        events = [
            {"type": "response.created", "response": {"id": "r1"}},
            {"type": "response.output_item.done", "item": item},
            {"type": "response.completed", "response": {"id": "r1", "usage": usage}},
        ]
        self.send_response(200)
        self.send_header("content-type", "text/event-stream")
        self.end_headers()
        for event in events:
            self.wfile.write(f"event: {event['type']}\ndata: {json.dumps(event)}\n\n".encode())


class _FakeChat(http.server.BaseHTTPRequestHandler):
    """Answer Chat Completions: one call of ``tool``, then a final message."""

    tool: tuple[str, dict[str, Any]] = ("read", {})
    tool_outputs: list[str] = []

    def log_message(self, *args: object) -> None:
        """Keep the test output quiet.

        Args:
            *args: The log values, which the server ignores.
        """

    def do_POST(self) -> None:
        """Stream one model turn as server-sent events."""
        body = json.loads(self.rfile.read(int(self.headers["Content-Length"])))
        outputs = [message["content"] for message in body.get("messages", []) if message.get("role") == "tool"]
        _FakeChat.tool_outputs = outputs
        if outputs or not body.get("tools"):
            delta: dict[str, Any] = {"role": "assistant", "content": "done"}
            finish = "stop"
        else:
            name, arguments = _FakeChat.tool
            call = {
                "index": 0,
                "id": "c1",
                "type": "function",
                "function": {"name": name, "arguments": json.dumps(arguments)},
            }
            delta = {"role": "assistant", "content": None, "tool_calls": [call]}
            finish = "tool_calls"
        usage = {"prompt_tokens": 1, "completion_tokens": 1, "total_tokens": 2}
        chunks = [
            {
                "id": "x",
                "object": "chat.completion.chunk",
                "model": "fake-model",
                "choices": [{"index": 0, "delta": delta}],
            },
            {
                "id": "x",
                "object": "chat.completion.chunk",
                "model": "fake-model",
                "choices": [{"index": 0, "delta": {}, "finish_reason": finish}],
                "usage": usage,
            },
        ]
        self.send_response(200)
        self.send_header("content-type", "text/event-stream")
        self.end_headers()
        for chunk in chunks:
            self.wfile.write(f"data: {json.dumps(chunk)}\n\n".encode())
        self.wfile.write(b"data: [DONE]\n\n")


def _fake_tool(bin_dir: Path, name: str, script: str) -> None:
    """Write an executable shell command into ``bin_dir``.

    Args:
        bin_dir: Folder for the command.
        name: The command name.
        script: Its shell body.
    """
    bin_dir.mkdir(parents=True, exist_ok=True)
    tool = bin_dir / name
    tool.write_text(f"#!/bin/sh\n{script}\n")
    tool.chmod(0o755)


def test_exec_shim_records_each_listed_command_once_and_passes_its_streams_through(tmp_path: Path) -> None:
    """The shim logs command line, exit code and output of a listed command, also nested, and keeps its behavior.

    Args:
        tmp_path: Pytest's temporary folder.
    """
    bin_dir = tmp_path / "bin"
    _fake_tool(bin_dir, "probe", 'echo "out $*"; echo err >&2; exit 3')
    _fake_tool(bin_dir, "outer", "probe inner; exit 0")
    proposer = {
        **_PLAIN,
        "run_command": 'probe "a b" 2>/dev/null; echo "rc=$?"; outer; ls >/dev/null',
        "env": {"PATH": f"{bin_dir}{os.pathsep}{os.environ['PATH']}"},
        "exec_commands": ["probe", "outer"],
    }
    session = tmp_path / "session"

    # The limit only stops a hung shim; on a loaded machine the shim's Python starts can take seconds (SKY-839).
    outcome = run_session(
        proposer, workspace=tmp_path / "work", prompt="x", model="m", session_dir=session, timeout_seconds=60
    )

    assert outcome.returncode == 0
    assert outcome.text == "out a b\nrc=3\nout inner"
    records = [json.loads(line) for line in (session / harness_bridge.EXEC_LOG).read_text().splitlines()]
    assert records == [
        {"command": "probe 'a b'", "exit_code": 3, "stdout": "out a b\n", "stderr": "err\n"},
        {"command": "outer", "exit_code": 0, "stdout": "out inner\n", "stderr": "err\n"},
    ]


def test_exec_shim_reports_a_missing_command_like_the_shell(tmp_path: Path) -> None:
    """A listed command that is not installed fails with 127 and is still logged.

    Args:
        tmp_path: Pytest's temporary folder.
    """
    proposer = {**_PLAIN, "run_command": 'no-such-tool-xyz; echo "rc=$?"', "exec_commands": ["no-such-tool-xyz"]}
    session = tmp_path / "session"

    outcome = run_session(
        proposer, workspace=tmp_path / "work", prompt="x", model="m", session_dir=session, timeout_seconds=10
    )

    assert outcome.text == "rc=127"
    [record] = [json.loads(line) for line in (session / harness_bridge.EXEC_LOG).read_text().splitlines()]
    assert (record["command"], record["exit_code"]) == ("no-such-tool-xyz", 127)


def test_run_session_without_exec_commands_installs_no_exec_shim(tmp_path: Path) -> None:
    """A launch that lists no command gets no shim folder and no log, and sees its session folder.

    Args:
        tmp_path: Pytest's temporary folder.
    """
    proposer = {**_PLAIN, "run_command": 'echo "$APP_SESSION_DIR"; echo "$PATH"'}
    session = tmp_path / "session"

    outcome = run_session(proposer, workspace=tmp_path / "work", prompt="x", model="m", session_dir=session)

    session_line, path_line = (outcome.text or "").splitlines()
    assert session_line == str(session)
    assert str(session / harness_bridge.EXEC_SHIM_DIR) not in path_line
    assert not (session / harness_bridge.EXEC_LOG).exists()


def _shim_env(tmp_path: Path, names: list[str], log: Path | None) -> dict[str, str]:
    """Install exec shim wrappers and return the environment that runs them.

    Args:
        tmp_path: Pytest's temporary folder; ``bin`` holds the real commands.
        names: The command names to wrap.
        log: The exec log, or ``None`` for a launch with no log.

    Returns:
        The process environment.
    """
    shim_dir = harness_bridge.install_exec_shim(tmp_path / "shim", names, sys.executable)
    env = {key: value for key, value in os.environ.items() if key != "APP_EXEC_LOG"}
    env.update({"PATH": f"{shim_dir}{os.pathsep}{tmp_path / 'bin'}{os.pathsep}{os.environ['PATH']}"})
    if log is not None:
        env["APP_EXEC_LOG"] = str(log)
    return env


def test_exec_shim_finds_the_real_command_when_the_env_drops_the_app_vars(tmp_path: Path) -> None:
    """A child env with no ``APP_*`` vars still skips the shim folder on PATH, so the shim does not run itself.

    Args:
        tmp_path: Pytest's temporary folder.
    """
    _fake_tool(tmp_path / "bin", "probe", "echo real; exit 5")
    env = {key: value for key, value in _shim_env(tmp_path, ["probe"], None).items() if not key.startswith("APP_")}

    done = subprocess.run(["probe"], env=env, capture_output=True, cwd=tmp_path, timeout=10, check=False)

    assert (done.returncode, done.stdout) == (5, b"real\n")


@pytest.mark.parametrize("logged", [True, False])
def test_exec_shim_finds_a_command_in_the_current_folder_through_an_empty_path_entry(
    tmp_path: Path, logged: bool
) -> None:
    """A shell and ``which`` read an empty PATH entry as the current folder, so the shim must too, and run it once.

    Args:
        tmp_path: Pytest's temporary folder.
        logged: Whether the shim writes an exec log.
    """
    _fake_tool(tmp_path / "here", "probe", "echo real; exit 5")
    shim_dir = harness_bridge.install_exec_shim(tmp_path / "shim", ["probe"], sys.executable)
    # The wrapper counts its runs: a bare-name match would make Popen find the wrapper on PATH again.
    calls, wrapper = tmp_path / "calls", shim_dir / "probe"
    wrapper.write_text(wrapper.read_text().replace("#!/bin/sh\n", f"#!/bin/sh\necho x >> {shlex.quote(str(calls))}\n"))
    log = tmp_path / "exec.jsonl"
    env = {key: value for key, value in os.environ.items() if not key.startswith("APP_")}
    env["PATH"] = f"{shim_dir}{os.pathsep}{os.pathsep}{os.environ['PATH']}"
    if logged:
        env["APP_EXEC_LOG"] = str(log)

    done = subprocess.run(["probe"], env=env, cwd=tmp_path / "here", capture_output=True, timeout=10, check=False)

    assert (done.returncode, done.stdout, done.stderr) == (5, b"real\n", b"")
    assert calls.read_text() == "x\n"
    if logged:
        [record] = [json.loads(line) for line in log.read_text().splitlines()]
        assert (record["command"], record["exit_code"]) == ("probe", 5)


@pytest.mark.parametrize("name", ["../x", "a/b", "..", ".", "", "x\0y"])
def test_install_exec_shim_rejects_a_name_that_is_not_plain_and_writes_nothing_outside(
    tmp_path: Path, name: str
) -> None:
    """A command name with a path part, or a NUL, raises ValueError before any file is written.

    Args:
        tmp_path: Pytest's temporary folder.
        name: A command name that is not plain.
    """
    shim_dir = tmp_path / "shim"

    with pytest.raises(ValueError, match="plain name"):
        harness_bridge.install_exec_shim(shim_dir, [name], sys.executable)

    assert sorted(path.relative_to(tmp_path).as_posix() for path in tmp_path.rglob("*")) == ["shim"]


def test_install_exec_shim_fails_when_exec_shim_py_is_missing(tmp_path: Path, monkeypatch) -> None:
    """With no ``exec_shim.py`` next to the bridge, the install raises FileNotFoundError and writes no wrapper.

    Args:
        tmp_path: Pytest's temporary folder.
        monkeypatch: Pytest monkeypatch.
    """
    monkeypatch.setattr(harness_bridge, "__file__", str(tmp_path / "bridge" / "harness_bridge.py"))

    with pytest.raises(FileNotFoundError, match="exec shim not found"):
        harness_bridge.install_exec_shim(tmp_path / "shim", ["probe"], sys.executable)

    assert not (tmp_path / "shim").exists()


def test_exec_shim_logs_only_the_last_64_kib_of_each_stream(tmp_path: Path) -> None:
    """The caller gets the full output; the log keeps its tail.

    Args:
        tmp_path: Pytest's temporary folder.
    """
    _fake_tool(tmp_path / "bin", "big", "head -c 70000 /dev/zero | tr '\\0' a; printf END")
    log = tmp_path / "exec.jsonl"

    done = subprocess.run(["big"], env=_shim_env(tmp_path, ["big"], log), capture_output=True, timeout=10, check=True)

    assert done.stdout == b"a" * 70000 + b"END"
    [record] = [json.loads(line) for line in log.read_text().splitlines()]
    assert record["stdout"] == "a" * (64 * 1024 - 3) + "END"


def test_exec_shim_without_a_log_only_passes_the_command_through(tmp_path: Path) -> None:
    """With no exec log set, the shim runs the real command with its streams and exit code, and writes nothing.

    Args:
        tmp_path: Pytest's temporary folder.
    """
    _fake_tool(tmp_path / "bin", "probe", "echo out; echo err >&2; exit 4")

    env = _shim_env(tmp_path, ["probe"], None)
    done = subprocess.run(["probe"], env=env, capture_output=True, cwd=tmp_path, timeout=10, check=False)

    assert (done.returncode, done.stdout, done.stderr) == (4, b"out\n", b"err\n")
    assert sorted(path.name for path in tmp_path.iterdir()) == ["bin", "shim"]


def test_exec_shim_streams_output_before_the_command_ends(tmp_path: Path) -> None:
    """A line the command prints reaches the caller while the command still runs.

    Args:
        tmp_path: Pytest's temporary folder.
    """
    flag = tmp_path / "flag"
    _fake_tool(tmp_path / "bin", "slow", f'echo first; while [ ! -f "{flag}" ]; do sleep 0.05; done; echo second')
    process = subprocess.Popen(
        ["slow"], env=_shim_env(tmp_path, ["slow"], tmp_path / "exec.jsonl"), stdout=subprocess.PIPE
    )
    # Ends the command if the shim holds the output, so the assertion below fails instead of hanging. The
    # wide times leave room for the shim's start on a loaded machine (SKY-839).
    timer = threading.Timer(30.0, flag.touch)
    # Ends a shim that never runs the command, so readline returns and the test fails.
    killer = threading.Timer(60.0, process.kill)
    timer.start()
    killer.start()
    try:
        assert process.stdout is not None
        assert process.stdout.readline() == b"first\n"
        assert not flag.exists()
    finally:
        flag.touch()
        timer.cancel()
        process.wait(timeout=10)
        killer.cancel()
    assert process.returncode == 0


def test_exec_shim_forwards_sigterm_and_logs_128_plus_the_signal(tmp_path: Path) -> None:
    """A SIGTERM to the shim ends the real command; the shim logs 128 plus the signal and dies from it.

    Args:
        tmp_path: Pytest's temporary folder.
    """
    _fake_tool(tmp_path / "bin", "hang", "echo ready; exec sleep 30")
    log = tmp_path / "exec.jsonl"
    process = subprocess.Popen(["hang"], env=_shim_env(tmp_path, ["hang"], log), stdout=subprocess.PIPE)
    # Ends a shim that never runs the command, so readline returns and the test fails.
    killer = threading.Timer(10.0, process.kill)
    killer.start()
    try:
        assert process.stdout is not None
        assert process.stdout.readline() == b"ready\n"
        process.send_signal(signal.SIGTERM)
        assert process.wait(timeout=10) == -signal.SIGTERM
    finally:
        killer.cancel()
        process.kill()
    [record] = [json.loads(line) for line in log.read_text().splitlines()]
    assert record["exit_code"] == 128 + signal.SIGTERM


@pytest.mark.parametrize("logged", [True, False])
def test_exec_shim_runs_a_script_with_no_shebang_line_with_sh(tmp_path: Path, logged: bool) -> None:
    """The shell runs a script with no shebang line with sh; the shim does too, with and without a log.

    Args:
        tmp_path: Pytest's temporary folder.
        logged: Whether the shim writes an exec log.
    """
    bin_dir, log = tmp_path / "bin", tmp_path / "exec.jsonl"
    bin_dir.mkdir()
    (bin_dir / "plain").write_text('echo "plain $1"\nexit 3\n')
    (bin_dir / "plain").chmod(0o755)

    env = _shim_env(tmp_path, ["plain"], log if logged else None)
    done = subprocess.run(["plain", "a"], env=env, capture_output=True, timeout=10, check=False)

    assert (done.returncode, done.stdout) == (3, b"plain a\n")
    if logged:
        [record] = [json.loads(line) for line in log.read_text().splitlines()]
        assert (record["command"], record["exit_code"], record["stdout"]) == ("plain a", 3, "plain a\n")


def test_exec_shim_logs_126_for_a_command_that_cannot_run(tmp_path: Path) -> None:
    """A command whose interpreter is missing exits 126, and its record is still written.

    Args:
        tmp_path: Pytest's temporary folder.
    """
    bin_dir, log = tmp_path / "bin", tmp_path / "exec.jsonl"
    bin_dir.mkdir()
    (bin_dir / "broken").write_text("#!/no/such/interpreter\n")
    (bin_dir / "broken").chmod(0o755)

    env = _shim_env(tmp_path, ["broken"], log)
    done = subprocess.run(["broken"], env=env, capture_output=True, timeout=10, check=False)

    assert done.returncode == 126
    assert done.stderr.startswith(b"broken: ")
    [record] = [json.loads(line) for line in log.read_text().splitlines()]
    assert (record["command"], record["exit_code"]) == ("broken", 126)


def test_exec_shim_passes_output_and_exit_code_through_when_the_log_cannot_be_written(tmp_path: Path) -> None:
    """An exec log path that is a folder loses the record, but the caller gets the real output and code, and no traceback.

    Args:
        tmp_path: Pytest's temporary folder.
    """
    _fake_tool(tmp_path / "bin", "probe", "echo out; echo err >&2; exit 4")
    log = tmp_path / "log-folder"
    log.mkdir()

    env = _shim_env(tmp_path, ["probe"], log)
    done = subprocess.run(["probe"], env=env, capture_output=True, timeout=10, check=False)

    assert (done.returncode, done.stdout, done.stderr) == (4, b"out\n", b"err\n")
    assert list(log.iterdir()) == []


def test_exec_shim_keeps_an_ignored_sigint_ignored_for_the_command(tmp_path: Path) -> None:
    """A shim started with SIGINT ignored (``cmd &`` under ``sh``) does not catch it, so the command inherits the ignore.

    Args:
        tmp_path: Pytest's temporary folder.
    """
    probe = "import signal; print(signal.getsignal(signal.SIGINT) is signal.SIG_IGN)"
    _fake_tool(tmp_path / "bin", "probe", f"exec {shlex.quote(sys.executable)} -c {shlex.quote(probe)}")
    log = tmp_path / "exec.jsonl"

    env = _shim_env(tmp_path, ["probe"], log)
    done = subprocess.run(
        ["/bin/sh", "-c", 'trap "" INT; exec probe'], env=env, capture_output=True, timeout=10, check=False
    )

    assert (done.returncode, done.stdout) == (0, b"True\n"), done.stderr
    [record] = [json.loads(line) for line in log.read_text().splitlines()]
    assert record["stdout"] == "True\n"


def test_exec_shim_ends_a_command_that_writes_forever_when_its_reader_closes(tmp_path: Path) -> None:
    """``cmd | head`` ends: the shim closes the child's pipe, so the child gets SIGPIPE and the log has 141.

    Args:
        tmp_path: Pytest's temporary folder.
    """
    _fake_tool(tmp_path / "bin", "inf", "exec yes")
    log = tmp_path / "exec.jsonl"
    process = subprocess.Popen(
        ["/bin/sh", "-c", "inf | head -c 5 >/dev/null; echo ok"],
        env=_shim_env(tmp_path, ["inf"], log),
        stdout=subprocess.PIPE,
        start_new_session=True,
    )
    try:
        stdout, _ = process.communicate(timeout=10)
    finally:
        # A shim that keeps the pipe open never ends; stop its whole group so the test fails and leaves nothing.
        with contextlib.suppress(ProcessLookupError):
            os.killpg(process.pid, signal.SIGKILL)
        process.wait(timeout=10)

    assert stdout == b"ok\n"
    [record] = [json.loads(line) for line in log.read_text().splitlines()]
    assert record["exit_code"] == 128 + signal.SIGPIPE


def test_exec_shim_exits_141_with_no_stderr_for_a_command_that_writes_line_by_line_into_a_closed_reader(
    tmp_path: Path,
) -> None:
    """A command that writes line by line into a closed reader ends with 141; the shim exits 141 and prints nothing to stderr.

    Args:
        tmp_path: Pytest's temporary folder.
    """
    _fake_tool(tmp_path / "bin", "lines", "while true; do echo hi; sleep 0.01; done")
    log = tmp_path / "exec.jsonl"
    process = subprocess.Popen(
        ["/bin/bash", "-c", 'lines | head -1 >/dev/null; echo "status ${PIPESTATUS[*]}"'],
        env=_shim_env(tmp_path, ["lines"], log),
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        start_new_session=True,
    )
    try:
        stdout, stderr = process.communicate(timeout=10)
    finally:
        with contextlib.suppress(ProcessLookupError):
            os.killpg(process.pid, signal.SIGKILL)
        process.wait(timeout=10)

    assert (stdout, stderr) == (f"status {128 + signal.SIGPIPE} 0\n".encode(), b"")
    [record] = [json.loads(line) for line in log.read_text().splitlines()]
    assert record["exit_code"] == 128 + signal.SIGPIPE


def test_exec_shim_logs_only_the_head_of_a_long_command_line_with_a_cut_marker(tmp_path: Path) -> None:
    """Each non-UTF-8 argv byte is six bytes in the JSON line, so the log keeps a bounded head and marks the cut.

    Args:
        tmp_path: Pytest's temporary folder.
    """
    _fake_tool(tmp_path / "bin", "probe", 'echo "$#"')
    log = tmp_path / "exec.jsonl"
    args = [b"\xff" * 60000] * 3

    env = _shim_env(tmp_path, ["probe"], log)
    done = subprocess.run([b"probe", *args], env=env, capture_output=True, timeout=10, check=True)

    assert done.stdout == b"3\n"
    [line] = log.read_bytes().splitlines()
    assert len(line) < 6 * exec_shim.COMMAND_LIMIT + 1024
    command = json.loads(line)["command"]
    assert command.endswith(exec_shim.COMMAND_CUT)
    head = command[: -len(exec_shim.COMMAND_CUT)].encode("utf-8", "surrogateescape")
    assert len(head) == exec_shim.COMMAND_LIMIT
    assert head.startswith(b"probe '\xff")


def test_exec_shim_passes_an_inherited_fd_3_to_the_command_and_none_of_its_own(tmp_path: Path) -> None:
    """``cmd 3>&1`` works through the shim as without it, and the command gets no fd of the shim's.

    Args:
        tmp_path: Pytest's temporary folder.
    """
    probe = (
        "import os\n"
        "os.write(3, b'sub\\n')\n"
        "open_fds = []\n"
        "for fd in range(3, 256):\n"
        "    try:\n"
        "        os.fstat(fd)\n"
        "    except OSError:\n"
        "        continue\n"
        "    open_fds.append(fd)\n"
        "print(open_fds)\n"
    )
    _fake_tool(tmp_path / "bin", "probe", f"exec {shlex.quote(sys.executable)} -c {shlex.quote(probe)}")
    log = tmp_path / "exec.jsonl"

    env = _shim_env(tmp_path, ["probe"], log)
    done = subprocess.run(["/bin/sh", "-c", "probe 3>&1"], env=env, capture_output=True, timeout=10, check=False)

    assert (done.returncode, done.stdout, done.stderr) == (0, b"sub\n[3]\n", b"")
    [record] = [json.loads(line) for line in log.read_text().splitlines()]
    assert (record["exit_code"], record["stdout"]) == (0, "[3]\n")


def test_exec_shim_with_its_stdout_closed_returns_the_command_code_and_writes_the_record(tmp_path: Path) -> None:
    """``cmd >&-`` leaves ``sys.stdout`` None; the shim still returns the command's code, with no traceback.

    Args:
        tmp_path: Pytest's temporary folder.
    """
    _fake_tool(tmp_path / "bin", "probe", "echo err >&2; exit 4")
    log = tmp_path / "exec.jsonl"

    env = _shim_env(tmp_path, ["probe"], log)
    done = subprocess.run(["/bin/sh", "-c", "probe >&-"], env=env, capture_output=True, timeout=10, check=False)

    assert (done.returncode, done.stderr) == (4, b"err\n")
    [record] = [json.loads(line) for line in log.read_text().splitlines()]
    assert (record["command"], record["exit_code"], record["stderr"]) == ("probe", 4, "err\n")


@pytest.mark.parametrize("mode", ["logged", "nested", "no-log"])
@pytest.mark.parametrize(
    ("command", "code", "stdout"), [("probe", 4, b"out\n"), ("nosuch", 127, b""), ("broken", 126, b"")]
)
def test_exec_shim_with_its_stderr_closed_keeps_stdout_clean_and_returns_the_command_code(
    tmp_path: Path, mode: str, command: str, code: int, stdout: bytes
) -> None:
    """``cmd 2>&-`` leaves ``sys.stderr`` None; no shim message goes to stdout, and the code is the command's.

    Args:
        tmp_path: Pytest's temporary folder.
        mode: How the shim runs: logged, nested or with no log.
        command: The command the shim runs.
        code: The exit code the shim must return.
        stdout: The output the shim must write.
    """
    _fake_tool(tmp_path / "bin", "probe", "echo out; echo err >&2; exit 4")
    (tmp_path / "bin" / "broken").write_text("#!/no/such/interpreter\n")
    (tmp_path / "bin" / "broken").chmod(0o755)
    log = tmp_path / "exec.jsonl"
    env = _shim_env(tmp_path, ["probe", "nosuch", "broken"], None if mode == "no-log" else log)
    if mode == "nested":
        env[exec_shim.ACTIVE_ENV] = "1"

    done = subprocess.run(["/bin/sh", "-c", f"{command} 2>&-"], env=env, capture_output=True, timeout=10, check=False)

    assert (done.returncode, done.stdout, done.stderr) == (code, stdout, b"")
    if mode == "logged":
        [record] = [json.loads(line) for line in log.read_text().splitlines()]
        assert (record["command"], record["exit_code"], record["stdout"]) == (command, code, stdout.decode())
    else:
        assert not log.exists()


@pytest.mark.parametrize("logged", [True, False])
@pytest.mark.parametrize(("closed", "kept"), [("2>&-", "stdout"), (">&-", "stderr")])
def test_exec_shim_with_a_stream_closed_lets_the_command_write_to_it_again_and_go_on(
    tmp_path: Path, logged: bool, closed: str, kept: str
) -> None:
    """With no shim, writes to a closed stream fail and the command goes on; the shim must not end it with SIGPIPE.

    Args:
        tmp_path: Pytest's temporary folder.
        logged: Whether the shim writes an exec log.
        closed: The shell form that closes a stream.
        kept: The stream that stays open.
    """
    lost = "1" if kept == "stderr" else "2"
    shown = "2" if kept == "stderr" else "1"
    _fake_tool(tmp_path / "bin", "tw", f"echo a >&{lost}; sleep 0.2; echo b >&{lost}; echo kept >&{shown}; exit 0")
    log = tmp_path / "exec.jsonl"

    env = _shim_env(tmp_path, ["tw"], log if logged else None)
    done = subprocess.run(["/bin/sh", "-c", f"tw {closed}"], env=env, capture_output=True, timeout=10, check=False)

    assert done.returncode == 0, done
    assert getattr(done, kept).endswith(b"kept\n")
    if logged:
        [record] = [json.loads(line) for line in log.read_text().splitlines()]
        lost_stream = "stdout" if kept == "stderr" else "stderr"
        assert (record["exit_code"], record[lost_stream], record[kept]) == (0, "a\nb\n", "kept\n")


@pytest.mark.parametrize("number", [signal.SIGTERM, signal.SIGKILL])
def test_exec_shim_dies_from_the_signal_that_ended_the_command(tmp_path: Path, number: signal.Signals) -> None:
    """A command that a signal ends makes the shim die from that signal, so a caller such as bash sees the signal.

    Args:
        tmp_path: Pytest's temporary folder.
        number: The signal that ends the command.
    """
    _fake_tool(tmp_path / "bin", "zz", f"echo out; kill -{number.name[3:]} $$")
    log = tmp_path / "exec.jsonl"

    done = subprocess.run(["zz"], env=_shim_env(tmp_path, ["zz"], log), capture_output=True, timeout=10, check=False)

    assert (done.returncode, done.stdout, done.stderr) == (-number, b"out\n", b"")
    [record] = [json.loads(line) for line in log.read_text().splitlines()]
    assert (record["exit_code"], record["stdout"]) == (128 + number, "out\n")


def test_exec_shim_exits_128_plus_a_core_dump_signal_and_does_not_die_from_it(tmp_path: Path) -> None:
    """A core-dump signal that ends the command gives exit 128 plus it, so the shim writes no core of its own.

    Args:
        tmp_path: Pytest's temporary folder.
    """
    # SIGXFSZ writes a core on Linux, but makes no crash report on macOS, so the test leaves nothing behind.
    _fake_tool(tmp_path / "bin", "zz", "echo out; kill -XFSZ $$")
    log = tmp_path / "exec.jsonl"

    done = subprocess.run(["zz"], env=_shim_env(tmp_path, ["zz"], log), capture_output=True, timeout=10, check=False)

    assert (done.returncode, done.stdout, done.stderr) == (128 + signal.SIGXFSZ, b"out\n", b"")
    [record] = [json.loads(line) for line in log.read_text().splitlines()]
    assert record["exit_code"] == 128 + signal.SIGXFSZ


def test_exec_shim_with_no_log_runs_the_command_with_sigpipe_at_its_default(tmp_path: Path) -> None:
    """Python ignores SIGPIPE, and an exec keeps that; the shim resets it, so ``yes | head -1`` ends ``yes`` with 141.

    Args:
        tmp_path: Pytest's temporary folder.
    """
    (tmp_path / "bin").mkdir()
    (tmp_path / "bin" / "pp").write_text('#!/bin/bash\nyes | head -1 >/dev/null; echo "status ${PIPESTATUS[*]}"\n')
    (tmp_path / "bin" / "pp").chmod(0o755)

    env = _shim_env(tmp_path, ["pp"], None)
    done = subprocess.run(["pp"], env=env, capture_output=True, timeout=10, check=False)

    assert (done.returncode, done.stdout, done.stderr) == (0, b"status 141 0\n", b"")


def test_a_name_that_starts_with_two_dots_can_be_hidden(tmp_path: Path) -> None:
    """Hide ``..data``: only a ``..`` part leaves the repository, not a name that starts with two dots.

    Args:
        tmp_path: Pytest's temporary folder.
    """
    hashes = harness_bridge.hidden_hashes(["..data"])

    assert harness_bridge.hashed_kind(hashes, "..data/answers.csv") is not None
    assert harness_bridge.hashed_kind(hashes, "../..data/answers.csv") is None
    assert harness_bridge.hashed_kind(hashes, "..") is None
    workspace = tmp_path / "work"
    workspace.mkdir()
    policy = harness_bridge.guard_policy(workspace, ["..data"], tmp_path / "refusals.jsonl")
    assert harness_bridge.guard_check(policy, "Read", {"file_path": "..data/answers.csv"}) is not None
    assert harness_bridge.guard_check(policy, "Read", {"file_path": f"{workspace}/..data/answers.csv"}) is not None
    assert harness_bridge.guard_check(policy, "Read", {"file_path": "../outside.csv"}) is None


def test_a_setup_that_runs_too_long_stops_with_every_process_it_started(tmp_path: Path) -> None:
    """The setup's whole process group stops at the deadline, and the message names the command (SKY-753).

    Args:
        tmp_path: The checkout and the child's pid file.
    """
    pid_file = tmp_path / "child.pid"
    script = (
        "import subprocess, sys, time\n"
        "child = subprocess.Popen([sys.executable, '-c', 'import time; time.sleep(60)'])\n"
        f"open({str(pid_file)!r}, 'w').write(str(child.pid))\n"
        "time.sleep(60)\n"
    )

    started = time.monotonic()
    with pytest.raises(
        RuntimeError, match=r"(?s)\[proposer\] setup command in ascent.toml .* did not finish within 2 seconds"
    ):
        harness_bridge.run_setup([sys.executable, "-c", script], tmp_path, timeout_seconds=2)

    assert time.monotonic() - started < 30
    child = int(pid_file.read_text())
    for _ in range(50):
        try:
            os.kill(child, 0)
        except ProcessLookupError:
            break
        time.sleep(0.1)
    else:
        pytest.fail("The setup's child process still runs.")


def test_the_setup_runs_once_and_a_reset_checkout_gets_its_output_back(tmp_path: Path) -> None:
    """``git clean -fdx`` removes the setup's output, and the next session gets a copy, not a new setup (SKY-753).

    Args:
        tmp_path: The checkout and the setup's log.
    """
    checkout = tmp_path / "checkout"
    checkout.mkdir()
    (checkout / "a [1].txt").write_text("tracked")
    for arguments in (["init", "--quiet"], ["add", "--all"], ["commit", "--quiet", "--no-verify", "-m", "base"]):
        subprocess.run(
            ["git", "-c", "user.name=t", "-c", "user.email=t@example.com", *arguments], cwd=checkout, check=True
        )
    (checkout / "parent.txt").write_text("from the parent version")
    log = tmp_path / "setup.log"
    command = [
        sys.executable,
        "-c",
        "import os; os.makedirs('deps/bin'); open('deps/bin/tool', 'w').write('t');"
        " os.symlink('bin/tool', 'deps/link'); open('out [x].txt', 'w').write('o');"
        f" open({str(log)!r}, 'a').write('ran\\n')",
    ]

    harness_bridge.ensure_setup(command, checkout)
    harness_bridge.ensure_setup(command, checkout)
    status = subprocess.run(
        ["git", "status", "--porcelain", "--untracked-files=all"],
        cwd=checkout,
        capture_output=True,
        text=True,
        check=True,
    ).stdout
    subprocess.run(["git", "clean", "-fdxq"], cwd=checkout, check=True)
    (checkout / "out [x].txt").write_text("from the next version")
    harness_bridge.ensure_setup(command, checkout)

    assert log.read_text() == "ran\n"
    # Only the setup's own output is hidden; the parent version's new file stays in the version.
    assert status == "?? parent.txt\n"
    assert (checkout / "deps/bin/tool").read_text() == "t"
    assert (checkout / "deps/link").readlink() == Path("bin/tool")
    # A file of the version at a setup path wins over the kept copy.
    assert (checkout / "out [x].txt").read_text() == "from the next version"


def test_a_tracked_file_with_the_old_marker_name_does_not_skip_the_setup(tmp_path: Path) -> None:
    """The setup's record is in the git dir, so no repository file can claim that the setup already ran (SKY-753).

    Args:
        tmp_path: The checkout and the setup's log.
    """
    checkout = tmp_path / "checkout"
    checkout.mkdir()
    for name in (".ascent-proposer-setup", harness_bridge.SETUP_STATE):
        (checkout / name).write_text("done")
    for arguments in (["init", "--quiet"], ["add", "--all"], ["commit", "--quiet", "--no-verify", "-m", "base"]):
        subprocess.run(
            ["git", "-c", "user.name=t", "-c", "user.email=t@example.com", *arguments], cwd=checkout, check=True
        )
    log = tmp_path / "setup.log"

    harness_bridge.ensure_setup([sys.executable, "-c", f"open({str(log)!r}, 'a').write('ran')"], checkout)

    assert log.read_text() == "ran"


@pytest.mark.parametrize("name", ["x\n*", "x\t", "x ", "x\r"])
def test_a_setup_file_name_that_no_exclude_line_can_hold_stops_the_run(tmp_path: Path, name: str) -> None:
    """A newline would write a ``*`` line that hides every file, and git drops trailing whitespace (SKY-753).

    Args:
        tmp_path: The checkout.
        name: A file name the setup writes.
    """
    checkout = _git_checkout(tmp_path / "checkout")
    command = [sys.executable, "-c", f"open({name!r}, 'w')"]

    with pytest.raises(RuntimeError, match=r"control character or end in whitespace: " + re.escape(repr(name))):
        harness_bridge.ensure_setup(command, checkout)

    exclude = checkout / ".git/info/exclude"
    assert "*" not in (exclude.read_text().splitlines() if exclude.exists() else [])
    assert not (checkout / ".git" / harness_bridge.SETUP_STATE / "done.json").exists()


def test_the_contract_files_command_keeps_a_key_token_as_text(tmp_path: Path) -> None:
    """The bridge fills the relay key into a harness command, but never into the file's own command (SKY-753).

    Args:
        tmp_path: The workspace and the session folder.
    """
    proposer = {**_PLAIN, "run_command": f"echo {harness_bridge.KEY_TOKEN}"}
    checkout = _git_checkout(tmp_path / "w")

    filled = run_session(proposer, workspace=checkout, prompt="x", model="m", session_dir=tmp_path / "s1", api_key="k")
    exact = run_session(
        {**proposer, "file_command": True},
        workspace=checkout,
        prompt="x",
        model="m",
        session_dir=tmp_path / "s2",
        api_key="k",
    )

    assert filled.stdout.strip() == "k"
    assert exact.stdout.strip() == harness_bridge.KEY_TOKEN


@pytest.mark.parametrize("where", ["run_command", "env", "files"])
@pytest.mark.parametrize("vendor", ["APP", "SKYNET"])
def test_a_saved_launch_with_a_key_token_from_before_sky_776_is_refused(
    tmp_path: Path, where: str, vendor: str
) -> None:
    """A resumed launch that still holds an old key token fails with an error that names the new token.

    Args:
        tmp_path: The workspace and the session folder.
        where: The part of the launch that holds the old token.
        vendor: The old token's prefix.
    """
    old = f"__{vendor}_API_KEY__"
    proposer: dict[str, Any] = {**_PLAIN, "run_command": "echo ok"}
    if where == "run_command":
        proposer["run_command"] = f"agent --key {old}"
    elif where == "env":
        proposer["env"] = {"TOKEN": old}
    else:
        proposer["files"] = {"config.json": json.dumps({"key": old})}

    with pytest.raises(RuntimeError, match=f"old key token {old}.*{harness_bridge.KEY_TOKEN}"):
        run_session(proposer, workspace=tmp_path / "w", prompt="x", model="m", session_dir=tmp_path / "s", api_key="k")

    assert not (tmp_path / "s" / "prompt-1.md").exists()


def test_a_launch_with_another_api_key_token_in_its_text_is_not_refused(tmp_path: Path) -> None:
    """Only the old Skynet key tokens are refused; other ``__*_API_KEY__`` text in a command stays as text.

    Args:
        tmp_path: The workspace and the session folder.
    """
    proposer = {**_PLAIN, "run_command": "echo __OPENAI_API_KEY__"}

    outcome = run_session(
        proposer, workspace=tmp_path / "w", prompt="x", model="m", session_dir=tmp_path / "s", api_key="k"
    )

    assert outcome.returncode == 0
    assert outcome.stdout.strip() == "__OPENAI_API_KEY__"


def test_a_finished_setup_stops_the_processes_it_left_in_the_background(tmp_path: Path) -> None:
    """A setup that exits with a daemon still running leaves nothing behind beside the sessions (SKY-753).

    Args:
        tmp_path: The checkout and the child's pid file.
    """
    pid_file = tmp_path / "child.pid"
    script = (
        "import subprocess, sys\n"
        "child = subprocess.Popen([sys.executable, '-c', 'import time; time.sleep(60)'],"
        " stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)\n"
        f"open({str(pid_file)!r}, 'w').write(str(child.pid))\n"
    )

    harness_bridge.run_setup([sys.executable, "-c", script], tmp_path)

    child = int(pid_file.read_text())
    for _ in range(50):
        try:
            os.kill(child, 0)
        except ProcessLookupError:
            break
        time.sleep(0.1)
    else:
        pytest.fail("The setup's background process still runs.")


def test_a_setup_ends_when_its_process_ends_and_not_when_a_background_child_closes_its_output(
    tmp_path: Path,
) -> None:
    """A background child that keeps the setup's stdout does not hold the setup until its timeout (SKY-753).

    Args:
        tmp_path: The checkout.
    """
    started = time.monotonic()

    harness_bridge.run_setup(["sh", "-c", "echo built; sleep 30 &"], tmp_path, timeout_seconds=20)

    assert time.monotonic() - started < 10


def test_a_failed_setup_names_its_output_in_the_error(tmp_path: Path) -> None:
    """The error of a failed setup carries the end of its output (SKY-753).

    Args:
        tmp_path: The checkout.
    """
    with pytest.raises(RuntimeError, match=r"failed with exit code 3: no such package"):
        harness_bridge.run_setup(["sh", "-c", "echo no such package >&2; exit 3"], tmp_path)


@pytest.mark.skipif(sys.platform != "linux", reason="PR_SET_CHILD_SUBREAPER is Linux-only")
def test_a_finished_setup_stops_a_process_that_left_its_group(tmp_path: Path) -> None:
    """A setup child that calls ``setsid`` stays under the subreaper and stops with the setup (SKY-753).

    Args:
        tmp_path: The checkout and the child's pid file.
    """
    pid_file = tmp_path / "job.pid"
    tool = (
        "import os, subprocess; os.setsid(); "
        "job = subprocess.Popen(['sleep', '300'], stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL); "
        f"open({str(pid_file)!r}, 'w').write(str(job.pid))"
    )
    job = None
    try:
        harness_bridge.run_setup(["sh", "-c", f"{shlex.quote(sys.executable)} -c {shlex.quote(tool)}"], tmp_path)
        job = int(pid_file.read_text())
        deadline = time.monotonic() + 10
        while _alive(job):
            assert time.monotonic() < deadline, "the setup's detached job outlived the setup"
            time.sleep(0.05)
    finally:
        if job is not None:
            with contextlib.suppress(ProcessLookupError):
                os.kill(job, signal.SIGKILL)


def test_the_setups_files_in_a_folder_of_the_parent_version_stay_out_of_the_version(tmp_path: Path) -> None:
    """A setup that writes into an untracked folder the parent version added excludes only its own files (SKY-753).

    Args:
        tmp_path: The checkout.
    """
    checkout = tmp_path / "checkout"
    checkout.mkdir()
    (checkout / "a.txt").write_text("tracked")
    for arguments in (["init", "--quiet"], ["add", "--all"], ["commit", "--quiet", "--no-verify", "-m", "base"]):
        subprocess.run(
            ["git", "-c", "user.name=t", "-c", "user.email=t@example.com", *arguments], cwd=checkout, check=True
        )
    (checkout / "out").mkdir()
    (checkout / "out/parent.txt").write_text("from the parent version")
    command = [
        sys.executable,
        "-c",
        "open('out/cache.bin', 'w'); __import__('os').makedirs('build'); open('build/x', 'w')",
    ]

    harness_bridge.ensure_setup(command, checkout)
    status = subprocess.run(
        ["git", "status", "--porcelain", "--untracked-files=all"],
        cwd=checkout,
        capture_output=True,
        text=True,
        check=True,
    ).stdout

    assert status == "?? out/parent.txt\n"
    exclude = (checkout / ".git/info/exclude").read_text().splitlines()
    assert {"/build/", "/out/cache.bin"} <= set(exclude)


def _git_checkout(checkout: Path) -> Path:
    """Make a one-commit repository with one tracked file, ``a.txt``.

    Args:
        checkout: The folder to make.

    Returns:
        The checkout.
    """
    checkout.mkdir()
    (checkout / "a.txt").write_text("tracked")
    for arguments in (["init", "--quiet"], ["add", "--all"], ["commit", "--quiet", "--no-verify", "-m", "base"]):
        subprocess.run(
            ["git", "-c", "user.name=t", "-c", "user.email=t@example.com", *arguments], cwd=checkout, check=True
        )
    return checkout


def test_a_setup_that_edits_a_tracked_file_stops_with_its_command_and_the_file(tmp_path: Path) -> None:
    """A setup may write only untracked files, because a tracked edit would join every version (SKY-753).

    Args:
        tmp_path: The checkout.
    """
    checkout = _git_checkout(tmp_path / "checkout")
    (checkout / "a.txt").write_text("the parent version's edit")
    command = [sys.executable, "-c", "open('a.txt', 'a').write('!'); open('new.txt', 'w')"]

    with pytest.raises(
        RuntimeError, match=r"(?s)setup command in ascent.toml \(.*a\.txt.*\) changed tracked files: a\.txt\."
    ):
        harness_bridge.ensure_setup(command, checkout)

    assert not (checkout / ".git" / harness_bridge.SETUP_STATE / "done.json").exists()


def test_a_setup_that_leaves_the_parent_versions_tracked_edits_alone_passes(tmp_path: Path) -> None:
    """A tracked edit that the parent version made before the setup is not the setup's change (SKY-753).

    Args:
        tmp_path: The checkout.
    """
    checkout = _git_checkout(tmp_path / "checkout")
    (checkout / "a.txt").write_text("the parent version's edit")

    harness_bridge.ensure_setup([sys.executable, "-c", "open('new.txt', 'w')"], checkout)

    assert (checkout / ".git" / harness_bridge.SETUP_STATE / "done.json").exists()


def test_a_files_command_outside_a_checkout_runs_the_default_agent(tmp_path: Path) -> None:
    """An AutoSaddler reflect folder is no checkout, so the shipped default agent answers there (SKY-753).

    Args:
        tmp_path: The plain folder and the session folder.
    """
    log = tmp_path / "setup.log"
    proposer = {
        **_PLAIN,
        "run_command": "echo file-entry",
        "file_command": True,
        "setup": [sys.executable, "-c", f"open({str(log)!r}, 'w')"],
        "outside_checkout": {**_PLAIN, "run_command": "echo default-agent"},
    }

    outcome = run_session(
        proposer, workspace=tmp_path / "plain", prompt="x", model="m", session_dir=tmp_path / "s", api_key="k"
    )

    assert outcome.stdout.strip() == "default-agent"
    assert not log.exists()


def test_a_files_command_outside_a_checkout_without_a_default_agent_stops(tmp_path: Path) -> None:
    """Without the default agent's launch, a session outside a checkout fails with a clear error (SKY-753).

    Args:
        tmp_path: The plain folder and the session folder.
    """
    proposer = {**_PLAIN, "run_command": "echo file-entry", "file_command": True}

    with pytest.raises(RuntimeError, match=r"entry command in ascent.toml runs only in a repository checkout"):
        run_session(proposer, workspace=tmp_path / "plain", prompt="x", model="m", session_dir=tmp_path / "s")


def test_the_setup_group_stops_before_the_setup_is_reaped(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    """The group kill comes while the ended setup is not reaped, so its pid cannot belong to another process (SKY-753).

    Args:
        tmp_path: The checkout.
        monkeypatch: Group kill spy.
    """
    seen: list[int | None] = []
    real = harness_bridge._stop_group

    def spy(process: subprocess.Popen[Any], signum: int, root_start: int | None) -> None:
        """Record whether the setup was reaped, then stop the group.

        Args:
            process: The setup.
            signum: The signal.
            root_start: The setup's start time.
        """
        seen.append(process.returncode)
        os.kill(process.pid, 0)
        real(process, signum, root_start)

    monkeypatch.setattr(harness_bridge, "_stop_group", spy)

    harness_bridge.run_setup([sys.executable, "-c", "pass"], tmp_path)

    assert seen == [None]


def test_sigterm_to_the_bridge_stops_the_setup_it_runs(tmp_path: Path) -> None:
    """Upstream stops a session with SIGTERM to the bridge alone, so the bridge kills the setup group (SKY-753).

    Args:
        tmp_path: The checkout, the bridge config and the setup's pid file.
    """
    checkout = _git_checkout(tmp_path / "checkout")
    pid_file = tmp_path / "setup.pid"
    setup = [
        sys.executable,
        "-c",
        f"import os, time; open({str(pid_file)!r}, 'w').write(str(os.getpid())); time.sleep(60)",
    ]
    config = tmp_path / "proposer.json"
    config.write_text(json.dumps({**_PLAIN, "file_command": True, "setup": setup}))
    bridge = subprocess.Popen(
        [sys.executable, str(Path(harness_bridge.__file__).resolve()), "--print", "x"],
        cwd=checkout,
        env={**os.environ, "HOME": str(tmp_path / "home"), harness_bridge.CONFIG_ENV: str(config)},
        stdout=subprocess.DEVNULL,
        stderr=subprocess.DEVNULL,
    )
    setup_pid = None
    try:
        deadline = time.monotonic() + 20
        while not pid_file.exists() or not pid_file.read_text():
            assert time.monotonic() < deadline, "the setup did not start"
            time.sleep(0.05)
        setup_pid = int(pid_file.read_text())
        bridge.send_signal(signal.SIGTERM)

        assert bridge.wait(timeout=10) == 128 + signal.SIGTERM
        deadline = time.monotonic() + 10
        while _running(setup_pid):
            assert time.monotonic() < deadline, "the setup outlived the bridge"
            time.sleep(0.05)
    finally:
        bridge.kill()
        if setup_pid is not None:
            with contextlib.suppress(ProcessLookupError):
                os.kill(setup_pid, signal.SIGKILL)


# Runs the bridge file as the reaper with a no-op prctl, so the Linux-only reaper path also runs on macOS.
_REAPER_PRELUDE = """
import ctypes, runpy, sys

class _Libc:
    def prctl(self, *args):
        return 0

ctypes.CDLL = lambda *args, **kwargs: _Libc()
sys.argv = sys.argv[1:]
runpy.run_path(sys.argv[0], run_name="__main__")
"""


@pytest.mark.parametrize(
    ("make_command", "reason"),
    [
        (lambda folder: [str(folder / "missing-tool")], "No such file or directory"),
        (lambda folder: [str(folder / "not-executable")], "Permission denied"),
    ],
    ids=["missing", "not-executable"],
)
def test_a_setup_the_reaper_cannot_start_says_so_without_a_traceback(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, make_command: Any, reason: str
) -> None:
    """A missing or non-executable setup command under the reaper gives the "could not start" error (SKY-753).

    Args:
        tmp_path: The checkout, the fake interpreter and the setup command.
        monkeypatch: Forces the reaper path.
        make_command: Builds the setup command in ``tmp_path``.
        reason: The start error the message must name.
    """
    (tmp_path / "not-executable").write_text("#!/bin/sh\necho never\n")
    prelude = tmp_path / "prelude.py"
    prelude.write_text(_REAPER_PRELUDE)
    interpreter = tmp_path / "python"
    # The reaper command passes ``-I`` first; the fake interpreter moves it before the prelude.
    interpreter.write_text(
        f'#!/bin/sh\nshift\nexec {shlex.quote(sys.executable)} -I {shlex.quote(str(prelude))} "$@"\n'
    )
    interpreter.chmod(0o755)
    monkeypatch.setattr(sys, "platform", "linux")
    monkeypatch.setattr(sys, "executable", str(interpreter))

    with pytest.raises(RuntimeError) as raised:
        harness_bridge.run_setup(make_command(tmp_path), tmp_path, timeout_seconds=20)

    message = str(raised.value)
    assert "could not start" in message
    assert reason in message
    assert "Traceback" not in message


def test_an_empty_untracked_folder_is_no_setup_output(tmp_path: Path) -> None:
    """An empty folder before the setup stays out of the excludes; only the files the setup puts in one are (SKY-753).

    Args:
        tmp_path: The checkout.
    """
    checkout = _git_checkout(tmp_path / "checkout")
    (checkout / "untouched").mkdir()
    (checkout / "out").mkdir()

    harness_bridge.ensure_setup([sys.executable, "-c", "open('out/x', 'w')"], checkout)
    (checkout / "untouched/proposed.txt").write_text("a later session's file")
    status = subprocess.run(
        ["git", "status", "--porcelain", "--untracked-files=all"],
        cwd=checkout,
        capture_output=True,
        text=True,
        check=True,
    ).stdout

    assert status == "?? untouched/proposed.txt\n"
    exclude = set((checkout / ".git/info/exclude").read_text().splitlines())
    assert "/out/x" in exclude
    assert "/out/" not in exclude
    assert not any(line.startswith("/untouched") for line in exclude)
    record = json.loads((checkout / ".git" / harness_bridge.SETUP_STATE / "done.json").read_text())
    assert record["output"] == ["out/x"]


@pytest.mark.parametrize("in_checkout", [True, False])
def test_a_resumed_files_command_session_restates_the_task(tmp_path: Path, in_checkout: bool) -> None:
    """The file's command has no resume protocol, so a resume restates the task, also for the default agent (SKY-753).

    Args:
        tmp_path: The workspace, the bridge config and the home folder.
        in_checkout: Run in a checkout (the file's command) or a plain folder (the default agent).
    """
    workspace = _git_checkout(tmp_path / "checkout") if in_checkout else tmp_path / "plain"
    workspace.mkdir(exist_ok=True)
    config = tmp_path / "proposer.json"
    config.write_text(
        json.dumps(
            {
                **_PLAIN,
                "file_command": True,
                "resume_command": "echo native-resume",
                "outside_checkout": {**_PLAIN, "resume_command": "echo native-resume"},
            }
        )
    )
    env = {**os.environ, "HOME": str(tmp_path / "home"), harness_bridge.CONFIG_ENV: str(config)}
    bridge = [sys.executable, str(Path(harness_bridge.__file__).resolve()), "--print"]

    def result(*args: str) -> str:
        """Run the bridge once and return its answer.

        Args:
            *args: Prompt and session arguments.

        Returns:
            The result document's ``result``.
        """
        done = subprocess.run(
            [*bridge, *args], cwd=workspace, env=env, capture_output=True, text=True, check=True, timeout=30
        )
        return json.loads(done.stdout)["result"]

    assert result("first task", "--session-id", "s1") == "first task"
    resumed = result("--resume", "s1", "keep going")

    assert resumed.startswith("first task")
    assert harness_bridge._RESUME_NOTE.strip() in resumed
    assert resumed.endswith("keep going")


def test_a_setup_that_changes_or_removes_an_untracked_file_of_the_version_stops(tmp_path: Path) -> None:
    """GEPA and Best-of-N leave a parent version's new files untracked; the setup may not touch them (SKY-753).

    Args:
        tmp_path: The checkout.
    """
    checkout = _git_checkout(tmp_path / "checkout")
    (checkout / "kept.txt").write_text("from the parent version")
    (checkout / "gone.txt").write_text("from the parent version")
    command = [
        sys.executable,
        "-c",
        "open('kept.txt', 'a').write('!'); __import__('os').remove('gone.txt'); open('new.txt', 'w')",
    ]

    with pytest.raises(
        RuntimeError,
        match=(
            r"(?s)setup command in ascent.toml \(.*\) changed or removed files of the version:"
            r" gone\.txt, kept\.txt\."
        ),
    ):
        harness_bridge.ensure_setup(command, checkout)

    assert not (checkout / ".git" / harness_bridge.SETUP_STATE / "done.json").exists()
    assert (checkout / "kept.txt").read_text() == "from the parent version"
    assert (checkout / "gone.txt").read_text() == "from the parent version"
    assert not (checkout / "new.txt").exists()


@pytest.mark.parametrize(
    ("tracked", "error"),
    [(False, "changed or removed files of the version: link"), (True, "changed tracked files: link")],
    ids=["untracked", "tracked-changed"],
)
def test_a_setup_that_points_a_link_of_the_version_at_a_new_target_stops(
    tmp_path: Path, tracked: bool, error: str
) -> None:
    """A new link target enters every version even when both targets hold the same bytes (SKY-753).

    Args:
        tmp_path: The checkout.
        tracked: Whether git tracks the link, which the parent version then changed.
        error: The expected error text.
    """
    checkout = _git_checkout(tmp_path / "checkout")
    (checkout / "one.txt").write_text("same")
    (checkout / "two.txt").write_text("same")
    if tracked:
        (checkout / "link").symlink_to("a.txt")
        for arguments in (["add", "link"], ["commit", "--quiet", "--no-verify", "-m", "link"]):
            subprocess.run(
                ["git", "-c", "user.name=t", "-c", "user.email=t@example.com", *arguments], cwd=checkout, check=True
            )
        (checkout / "link").unlink()
    (checkout / "link").symlink_to("one.txt")
    command = [
        sys.executable,
        "-c",
        "import os; os.remove('link'); os.symlink('two.txt', 'link'); open('new.txt', 'w')",
    ]

    with pytest.raises(RuntimeError, match=rf"(?s)setup command in ascent.toml \(.*\) {error}\."):
        harness_bridge.ensure_setup(command, checkout)

    assert not (checkout / ".git" / harness_bridge.SETUP_STATE / "done.json").exists()


def test_the_setup_counts_inside_the_session_time_limit(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    """A setup cannot run past the session's limit, and the harness gets only the time the setup left (SKY-753).

    Args:
        tmp_path: The checkouts and the session folders.
        monkeypatch: Gives the second session a setup that takes a fixed time.
    """
    slow = {**_PLAIN, "setup": [sys.executable, "-c", "import time; time.sleep(30)"]}
    started = time.monotonic()
    # The git reads and the copy before the setup count inside the limit too, so a loaded machine needs
    # more than 1 s for them (SKY-839). The 30 s setup still shows that the limit stops it.
    with pytest.raises(RuntimeError, match=r"did not finish within (5|[0-4](\.\d+)?) seconds"):
        run_session(
            slow,
            workspace=_git_checkout(tmp_path / "slow"),
            prompt="x",
            model="m",
            session_dir=tmp_path / "s1",
            timeout_seconds=5.0,
            api_key="k",
        )
    assert time.monotonic() - started < 20

    # A real setup process can take far longer than its sleep to start under load (SKY-839), and then
    # it uses up the whole limit. A setup of a fixed length keeps the test on the harness's share.
    limit, setup = 8.0, 6.0
    monkeypatch.setattr(harness_bridge, "ensure_setup", lambda command, checkout, seconds, env: time.sleep(setup))
    proposer = {**_PLAIN, "run_command": "sleep 30", "setup": ["true"]}
    outcome = run_session(
        proposer,
        workspace=_git_checkout(tmp_path / "checkout"),
        prompt="x",
        model="m",
        session_dir=tmp_path / "s2",
        timeout_seconds=limit,
        api_key="k",
    )

    assert outcome.timed_out
    # The duration counts only the harness. With the full limit it would run near 8 s; the 2 s the
    # setup left stops it near 2 s, and the bound leaves 3 s each way for a loaded machine.
    assert outcome.duration_seconds < limit - setup + 3


def test_a_setup_that_uses_up_the_session_time_stops_before_the_harness_starts(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """A harness with no time left would be killed at once, so the session raises instead (SKY-753).

    Args:
        tmp_path: The checkout, the session folder and the harness marker.
        monkeypatch: Makes the setup end just past the session's limit.
    """
    ran = tmp_path / "harness-ran"
    monkeypatch.setattr(
        harness_bridge, "ensure_setup", lambda command, checkout, seconds, env: time.sleep(seconds + 0.2)
    )
    proposer = {**_PLAIN, "run_command": f"touch {shlex.quote(str(ran))}", "setup": ["true"]}

    with pytest.raises(RuntimeError, match=r"\(true\) used up the session's time limit of 0\.5 seconds"):
        run_session(
            proposer,
            workspace=_git_checkout(tmp_path / "checkout"),
            prompt="x",
            model="m",
            session_dir=tmp_path / "s",
            timeout_seconds=0.5,
            api_key="k",
        )

    assert not ran.exists()


class _ExitCalledError(Exception):
    """Stands in for ``os._exit`` so a test sees the bridge's stop without leaving pytest."""


@pytest.mark.parametrize("starter", ["setup", "session"])
def test_a_stop_that_lands_while_a_group_starts_still_kills_the_group(
    starter: str, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """SIGTERM between the start of a group and its record waits for the record, then kills the group (SKY-753).

    Args:
        starter: Whether the group is the setup's or the harness session's.
        tmp_path: The checkout and the session folder.
        monkeypatch: The signal at the start, a fake ``os._exit`` and a fresh deferred-stop record.
    """
    real_popen = subprocess.Popen
    started: list[subprocess.Popen[Any]] = []

    def popen(*args: Any, **kwargs: Any) -> subprocess.Popen[Any]:
        """Start the process, then deliver the stop before the bridge records its group.

        Args:
            *args: Arguments for :class:`subprocess.Popen`.
            **kwargs: Keyword arguments for :class:`subprocess.Popen`.

        Returns:
            The started process.
        """
        process = real_popen(*args, **kwargs)
        started.append(process)
        if len(started) == 1:
            harness_bridge._stop_owned_groups(signal.SIGTERM, None)
        return process

    def exit_(code: int) -> None:
        """Raise in place of ending the process.

        Args:
            code: The exit code the bridge asks for.

        Raises:
            _ExitCalledError: Always.
        """
        raise _ExitCalledError(code)

    checkout = _git_checkout(tmp_path / "checkout")
    monkeypatch.setattr(harness_bridge, "_DEFERRED_STOP", [])
    monkeypatch.setattr(subprocess, "Popen", popen)
    monkeypatch.setattr(os, "_exit", exit_)
    try:
        with pytest.raises(_ExitCalledError):
            if starter == "setup":
                harness_bridge.run_setup(["sleep", "30"], checkout)
            else:
                run_session(
                    {**_PLAIN, "run_command": "sleep 30"},
                    workspace=checkout,
                    prompt="x",
                    model="m",
                    session_dir=tmp_path / "s",
                    api_key="k",
                )

        assert started[0].wait(timeout=10) == -signal.SIGKILL
    finally:
        for process in started:
            with contextlib.suppress(ProcessLookupError):
                os.killpg(process.pid, signal.SIGKILL)
            process.wait()


def test_a_reset_checkout_gets_the_setups_ignored_output_back(tmp_path: Path) -> None:
    """``git clean -fdx`` also drops an ignored ``node_modules/``, so the kept copy holds it too (SKY-753).

    Args:
        tmp_path: The checkout and the setup's log.
    """
    checkout = _git_checkout(tmp_path / "checkout")
    (checkout / ".gitignore").write_text("node_modules/\n")
    for arguments in (["add", "--all"], ["commit", "--quiet", "--no-verify", "-m", "ignore"]):
        subprocess.run(
            ["git", "-c", "user.name=t", "-c", "user.email=t@example.com", *arguments], cwd=checkout, check=True
        )
    log = tmp_path / "setup.log"
    command = [
        sys.executable,
        "-c",
        "import os; os.makedirs('node_modules/pkg'); open('node_modules/pkg/index.js', 'w').write('js');"
        f" open({str(log)!r}, 'a').write('ran\\n')",
    ]

    harness_bridge.ensure_setup(command, checkout)
    subprocess.run(["git", "reset", "--hard", "--quiet"], cwd=checkout, check=True)
    subprocess.run(["git", "clean", "-fdxq"], cwd=checkout, check=True)
    assert not (checkout / "node_modules").exists()
    harness_bridge.ensure_setup(command, checkout)

    assert log.read_text() == "ran\n"
    assert (checkout / "node_modules/pkg/index.js").read_text() == "js"
    # A version can drop the .gitignore line, so the ignored folder gets an exclude line as well.
    assert "/node_modules/" in (checkout / ".git/info/exclude").read_text().splitlines()


def test_ignored_setup_output_stays_out_of_a_version_that_drops_its_gitignore_line(tmp_path: Path) -> None:
    """A version that removes ``deps/`` from .gitignore must not take the restored setup output in (SKY-753).

    Args:
        tmp_path: The checkout.
    """
    checkout = tmp_path / "checkout"
    checkout.mkdir()
    (checkout / ".gitignore").write_text("deps/\n")
    for arguments in (["init", "--quiet"], ["add", "--all"], ["commit", "--quiet", "--no-verify", "-m", "ignore"]):
        subprocess.run(
            ["git", "-c", "user.name=t", "-c", "user.email=t@example.com", *arguments], cwd=checkout, check=True
        )
    command = [sys.executable, "-c", "import os; os.makedirs('deps'); open('deps/lib.js', 'w').write('js')"]

    harness_bridge.ensure_setup(command, checkout)
    subprocess.run(["git", "clean", "-fdxq"], cwd=checkout, check=True)
    (checkout / ".gitignore").write_text("")
    harness_bridge.ensure_setup(command, checkout)
    status = subprocess.run(
        ["git", "status", "--porcelain", "--untracked-files=all"],
        cwd=checkout,
        capture_output=True,
        text=True,
        check=True,
    ).stdout

    assert (checkout / "deps/lib.js").read_text() == "js"
    assert status == " M .gitignore\n"


def test_a_stop_while_the_setup_record_is_written_leaves_no_broken_record(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """A stop during the record's write leaves no empty ``done.json``, so the next session runs the setup (SKY-753).

    Args:
        tmp_path: The checkout and the setup's log.
        monkeypatch: Pytest monkeypatch.
    """
    checkout = _git_checkout(tmp_path / "checkout")
    log = tmp_path / "setup.log"
    command = [sys.executable, "-c", f"open('dep.txt', 'w').write('d'); open({str(log)!r}, 'a').write('ran\\n')"]
    write_text = Path.write_text

    def stop_after_truncate(self: Path, *args: Any, **kwargs: Any) -> int:
        """Truncate a record file and stop before its content is written, as a SIGTERM there does.

        Args:
            self: The file to write.
            *args: The content and other arguments of :meth:`Path.write_text`.
            **kwargs: The keyword arguments of :meth:`Path.write_text`.

        Returns:
            The number of characters written, for any other file.

        Raises:
            KeyboardInterrupt: For the setup's record.
        """
        if self.name == "done.json.tmp":
            self.open("w").close()
            raise KeyboardInterrupt
        return write_text(self, *args, **kwargs)

    monkeypatch.setattr(Path, "write_text", stop_after_truncate)
    with pytest.raises(KeyboardInterrupt):
        harness_bridge.ensure_setup(command, checkout)
    monkeypatch.undo()
    harness_bridge.ensure_setup(command, checkout)

    assert log.read_text() == "ran\nran\n"
    assert "output" in json.loads((checkout / ".git" / harness_bridge.SETUP_STATE / "done.json").read_text())


def test_an_unreadable_setup_record_stops_with_a_clear_error(tmp_path: Path) -> None:
    """A broken ``done.json`` names the setup and the record, not a bare JSON error (SKY-753).

    Args:
        tmp_path: The checkout.
    """
    checkout = _git_checkout(tmp_path / "checkout")
    command = [sys.executable, "-c", "open('dep.txt', 'w')"]
    harness_bridge.ensure_setup(command, checkout)
    (checkout / ".git" / harness_bridge.SETUP_STATE / "done.json").write_text("")

    with pytest.raises(
        RuntimeError, match=r"(?s)record of the \[proposer\] setup command in ascent.toml \(.*\) at .*done\.json"
    ):
        harness_bridge.ensure_setup(command, checkout)


def test_a_session_waits_for_another_sessions_setup_only_up_to_its_time_limit(tmp_path: Path) -> None:
    """A session whose limit ends while another session's setup holds the lock stops with a clear error (SKY-753).

    Args:
        tmp_path: The checkout.
    """
    checkout = _git_checkout(tmp_path / "checkout")
    state = checkout / ".git" / harness_bridge.SETUP_STATE
    state.mkdir()
    started = time.monotonic()
    with (state / "lock").open("a") as other:
        fcntl.flock(other, fcntl.LOCK_EX)
        error = _finish_in_thread(
            lambda: harness_bridge.ensure_setup([sys.executable, "-c", "pass"], checkout, timeout_seconds=0.5)
        )

    assert isinstance(error, RuntimeError)
    assert re.search(r"did not finish in another session within 0\.5 seconds", str(error))
    assert time.monotonic() - started < 10


def test_a_waiter_runs_a_failed_setup_again_only_in_the_time_its_limit_has_left(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """The wait for a setup that then fails counts inside the waiter's limit, so it never runs a full limit more.

    Args:
        tmp_path: The checkout and the holder's start and release markers.
        monkeypatch: Spies on the limit and the run time of each ``run_setup``.
    """
    checkout = _git_checkout(tmp_path / "checkout")
    marker = tmp_path / "holder-started"
    release = tmp_path / "holder-release"
    limit = 6.0
    # The test, not a sleep in the holder, ends the holder's setup, so load can only make the wait longer.
    # The holder's exit and its undo can take seconds under load; the limit leaves them that room.
    held = 2.0
    holder_command = [
        sys.executable,
        "-c",
        f"import os, time; open({str(marker)!r}, 'w')\n"
        f"while not os.path.exists({str(release)!r}): time.sleep(0.02)\n"
        "raise SystemExit(1)",
    ]
    real_run_setup = harness_bridge.run_setup
    runs: list[tuple[list[str], float, float]] = []

    def timed_run_setup(command: list[str], path: Path, timeout: float, env: Mapping[str, str] | None) -> None:
        """Run the real setup, and keep its limit and how long it ran.

        Args:
            command: The setup command.
            path: The checkout.
            timeout: The limit that ``ensure_setup`` gave the setup.
            env: The setup's environment.
        """
        begun = time.monotonic()
        try:
            real_run_setup(command, path, timeout, env)
        finally:
            runs.append((command, timeout, time.monotonic() - begun))

    real_common_dir = harness_bridge._common_dir
    waiter_found_git_dir = threading.Event()

    def spied_common_dir(path: Path) -> Path:
        """Find the git dir, and tell the test when the waiter has it.

        Args:
            path: The checkout.

        Returns:
            The real git common dir.
        """
        common = real_common_dir(path)
        if threading.current_thread().name == "setup-waiter":
            waiter_found_git_dir.set()
        return common

    monkeypatch.setattr(harness_bridge, "run_setup", timed_run_setup)
    monkeypatch.setattr(harness_bridge, "_common_dir", spied_common_dir)
    holder = threading.Thread(
        target=lambda: _run_and_keep_error(lambda: harness_bridge.ensure_setup(holder_command, checkout), []),
        daemon=True,
    )
    holder.start()
    waiter_command = [sys.executable, "-c", "import time; time.sleep(30)"]
    try:
        deadline = time.monotonic() + 20
        while not marker.exists():
            assert time.monotonic() < deadline, "the holder's setup did not start"
            time.sleep(0.05)
        errors: list[BaseException] = []
        waiter = threading.Thread(
            target=_run_and_keep_error,
            args=(lambda: harness_bridge.ensure_setup(waiter_command, checkout, timeout_seconds=limit), errors),
            name="setup-waiter",
            daemon=True,
        )
        waiter.start()
        # ``ensure_setup`` starts the waiter's limit only after its git dir lookup, which is slow under load.
        # Counting from that lookup, the waiter waits for the lock at least this long.
        assert waiter_found_git_dir.wait(timeout=20), "the waiter did not find the git dir"
        time.sleep(held)
    finally:
        release.touch()
    waiter.join(timeout=30)
    holder.join(timeout=10)

    assert not waiter.is_alive()
    assert not holder.is_alive()
    assert len(errors) == 1
    error = errors[0]
    assert isinstance(error, RuntimeError)
    [(_, given, ran)] = [run for run in runs if run[0] == waiter_command]
    assert 0 < given <= limit - held
    assert f"did not finish within {given:g} seconds" in str(error)
    # A full limit would run the 30 s setup for 6 s; the time left stops it by about 4 s.
    assert ran < limit


def test_a_setup_with_no_time_left_after_the_lock_never_runs(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    """When the checks before the setup use up the limit, the session fails and does not run the setup.

    Args:
        tmp_path: The checkout and the setup's marker file.
        monkeypatch: Slows the tracked-file check past the limit and spies on ``run_setup``.
    """
    checkout = _git_checkout(tmp_path / "checkout")
    marker = tmp_path / "setup-ran"
    tracked_changes = harness_bridge._tracked_changes
    calls: list[float] = []

    def slow_tracked_changes(path: Path) -> dict[str, str]:
        """Use up the session's limit, then list the tracked changes.

        Args:
            path: The checkout.

        Returns:
            The real tracked changes.
        """
        time.sleep(0.5)
        return tracked_changes(path)

    monkeypatch.setattr(harness_bridge, "_tracked_changes", slow_tracked_changes)
    monkeypatch.setattr(harness_bridge, "run_setup", lambda command, path, timeout, env: calls.append(timeout))

    with pytest.raises(RuntimeError, match=r"no time was left within 0\.2 seconds"):
        harness_bridge.ensure_setup(
            [sys.executable, "-c", f"open({str(marker)!r}, 'w')"], checkout, timeout_seconds=0.2
        )

    assert calls == []
    assert not marker.exists()


def _run_and_keep_error(call: Any, errors: list[BaseException]) -> None:
    """Run ``call`` and append the exception it raises, if any.

    Args:
        call: The function to run.
        errors: Gets the exception.
    """
    try:
        call()
    except BaseException as error:
        errors.append(error)


def _finish_in_thread(call: Any, timeout_seconds: float = 20.0) -> BaseException | None:
    """Run ``call`` in a thread, so a lock wait that blocks fails the test instead of hanging it.

    Args:
        call: The function to run.
        timeout_seconds: Fail when the thread is still running after this long.

    Returns:
        The exception ``call`` raised, or ``None``.
    """
    errors: list[BaseException] = []
    thread = threading.Thread(target=_run_and_keep_error, args=(call, errors), daemon=True)
    thread.start()
    thread.join(timeout=timeout_seconds)
    assert not thread.is_alive(), "The call did not finish; a lock wait blocked."
    return errors[0] if errors else None


def test_a_later_session_copies_the_setup_output_without_the_lock(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Parallel sessions copy the kept output at the same time; only the first setup holds the lock (SKY-753).

    Args:
        tmp_path: The checkout.
        monkeypatch: Pytest monkeypatch.
    """
    checkout = tmp_path / "checkout"
    checkout.mkdir()
    (checkout / "tracked.txt").write_text("tracked")
    for arguments in (["init", "--quiet"], ["add", "--all"], ["commit", "--quiet", "--no-verify", "-m", "base"]):
        subprocess.run(
            ["git", "-c", "user.name=t", "-c", "user.email=t@example.com", *arguments], cwd=checkout, check=True
        )
    harness_bridge.ensure_setup([sys.executable, "-c", "open('dep.txt', 'w').write('d')"], checkout)
    subprocess.run(["git", "clean", "-fdxq"], cwd=checkout, check=True)
    copy = harness_bridge._copy_path
    lock_free: list[bool] = []

    def copy_while_another_session_locks(source: Path, target: Path, root: Path) -> None:
        """Take the setup lock from another open file, as a parallel session does, then copy.

        Args:
            source: The kept copy.
            target: The path in the checkout.
            root: The checkout.
        """
        with (checkout / ".git" / harness_bridge.SETUP_STATE / "lock").open("a") as other:
            try:
                fcntl.flock(other, fcntl.LOCK_EX | fcntl.LOCK_NB)
                lock_free.append(True)
            except BlockingIOError:
                lock_free.append(False)
        copy(source, target, root)

    monkeypatch.setattr(harness_bridge, "_copy_path", copy_while_another_session_locks)
    harness_bridge.ensure_setup([sys.executable, "-c", "raise SystemExit(1)"], checkout)

    assert lock_free == [True]
    assert (checkout / "dep.txt").read_text() == "d"


def test_a_setup_that_makes_a_commit_stops_with_its_command(tmp_path: Path) -> None:
    """A commit moves HEAD and hides its tracked change from the diff, but it would enter every version (SKY-753).

    Args:
        tmp_path: The checkout.
    """
    checkout = _git_checkout(tmp_path / "checkout")
    command = [
        "sh",
        "-c",
        "echo evil >> a.txt && git -c user.name=t -c user.email=t@example.com commit --quiet --no-verify -am s",
    ]

    with pytest.raises(RuntimeError, match=r"(?s)setup command in ascent.toml \(.*\) moved HEAD from [0-9a-f]+ to"):
        harness_bridge.ensure_setup(command, checkout)

    assert not (checkout / ".git" / harness_bridge.SETUP_STATE / "done.json").exists()


def test_a_setup_that_makes_a_commit_is_reset_to_the_old_head(tmp_path: Path) -> None:
    """A failed setup's commit is undone, with its tracked edit and its committed new file (SKY-753).

    Args:
        tmp_path: The checkout.
    """
    checkout = _git_checkout(tmp_path / "checkout")
    (checkout / "a.txt").write_text("the parent version's edit")
    (checkout / "parent.txt").write_text("from the parent version")

    def git(*arguments: str) -> str:
        """Run one git command in the checkout.

        Args:
            *arguments: Arguments after ``git``.

        Returns:
            Its standard output.
        """
        return subprocess.run(["git", *arguments], cwd=checkout, capture_output=True, text=True, check=True).stdout

    head = git("rev-parse", "HEAD")
    command = [
        "sh",
        "-c",
        "echo evil >> a.txt && echo c > committed.txt && echo n > new.txt && git add a.txt committed.txt"
        " && git -c user.name=t -c user.email=t@example.com commit --quiet --no-verify -m s",
    ]

    with pytest.raises(RuntimeError, match="moved HEAD"):
        harness_bridge.ensure_setup(command, checkout)

    assert git("rev-parse", "HEAD") == head
    assert git("status", "--porcelain", "--untracked-files=all") == " M a.txt\n?? parent.txt\n"
    assert (checkout / "a.txt").read_text() == "the parent version's edit"
    assert not (checkout / ".git" / harness_bridge.SETUP_STATE / "done.json").exists()


@pytest.mark.parametrize("parent_edit", [False, True], ids=["clean", "parent-edit"])
def test_a_setup_that_edits_a_tracked_file_is_undone_and_fails_again(tmp_path: Path, parent_edit: bool) -> None:
    """The refused edit never becomes the next setup's baseline, so a second session refuses it too (SKY-753).

    Args:
        tmp_path: The checkout.
        parent_edit: Whether the parent version changed the tracked file before the setup.
    """
    checkout = _git_checkout(tmp_path / "checkout")
    if parent_edit:
        (checkout / "a.txt").write_text("the parent version's edit")
    content = (checkout / "a.txt").read_text()
    command = [sys.executable, "-c", "open('a.txt', 'a').write('!'); open('new.txt', 'w')"]

    for _ in range(2):
        with pytest.raises(RuntimeError, match=r"changed tracked files: a\.txt\."):
            harness_bridge.ensure_setup(command, checkout)

        assert (checkout / "a.txt").read_text() == content
        assert not (checkout / "new.txt").exists()
        assert not (checkout / ".git" / harness_bridge.SETUP_STATE / "done.json").exists()


def test_an_undo_after_a_setup_links_a_tracked_folder_outside_deletes_nothing_there(tmp_path: Path) -> None:
    """The undo removes the setup's link before it restores the folder's files, so no outside file goes (SKY-753).

    Args:
        tmp_path: The checkout and the outside folder.
    """
    checkout = _git_checkout(tmp_path / "checkout")
    (checkout / "pkg").mkdir()
    (checkout / "pkg" / "x").write_text("tracked")
    for arguments in (["add", "--all"], ["commit", "--quiet", "--no-verify", "-m", "pkg"]):
        subprocess.run(
            ["git", "-c", "user.name=t", "-c", "user.email=t@example.com", *arguments], cwd=checkout, check=True
        )
    outside = tmp_path / "outside"
    outside.mkdir()
    (outside / "x").write_text("outside")
    command = [sys.executable, "-c", f"import os, shutil; shutil.rmtree('pkg'); os.symlink({str(outside)!r}, 'pkg')"]

    with pytest.raises(RuntimeError, match="changed tracked files"):
        harness_bridge.ensure_setup(command, checkout)

    assert (outside / "x").read_text() == "outside"
    assert not (checkout / "pkg").is_symlink()
    assert (checkout / "pkg" / "x").read_text() == "tracked"


def test_a_restore_into_a_parent_path_that_is_a_file_stops(tmp_path: Path) -> None:
    """A version that puts a file where the output's folder was gets a clear error, not a raw OSError (SKY-753).

    Args:
        tmp_path: The source and the root.
    """
    source = tmp_path / "out.txt"
    source.write_text("o")
    root = tmp_path / "checkout"
    root.mkdir()
    (root / "pkg").write_text("a file of the version")

    with pytest.raises(RuntimeError, match=r"pkg/gen/out\.txt cannot be restored: its parent folder .*pkg is a link"):
        harness_bridge._copy_path(source, root / "pkg" / "gen" / "out.txt", root)

    assert (root / "pkg").read_text() == "a file of the version"


def test_a_setup_file_name_that_is_not_utf8_stops_the_run() -> None:
    """Git output decodes a non-UTF-8 name to lone surrogates, which the UTF-8 exclude file cannot hold (SKY-753)."""
    name = b"dep\xff.bin".decode("utf-8", "surrogateescape")

    with pytest.raises(RuntimeError, match=r"not UTF-8, hold a control character or end in whitespace: 'dep\\udcff"):
        harness_bridge._excludable([name, "ok.txt"], ["npm", "ci"])


def test_the_command_reads_the_task_file_from_an_absolute_ascent_task(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """``ASCENT_TASK`` is an absolute path to the task file, also for a relative session folder (SKY-753).

    Args:
        tmp_path: The workspace and the session folder.
        monkeypatch: Moves into ``tmp_path`` and clears an inherited ``ASCENT_TASK``.
    """
    monkeypatch.chdir(tmp_path)
    monkeypatch.delenv(harness_bridge.TASK_ENV, raising=False)
    (tmp_path / "s").mkdir()
    proposer = {
        **_PLAIN,
        "run_command": f'case "${harness_bridge.TASK_ENV}" in /*) test -f "${harness_bridge.TASK_ENV}"'
        f' && cat "${harness_bridge.TASK_ENV}";; esac',
    }

    outcome = run_session(
        proposer, workspace=_git_checkout(tmp_path / "w"), prompt="the task", model="m", session_dir=Path("s")
    )

    assert outcome.stdout.strip() == "the task"


def test_the_setup_gets_the_proposer_env_and_the_guard(tmp_path: Path) -> None:
    """The setup is untrusted like the entry command, so it runs with the same environment and guard (SKY-753).

    Args:
        tmp_path: The checkout, the session folder and the setup's record of its environment.
    """
    seen = tmp_path / "seen.json"
    script = (
        "import json, os; json.dump({name: os.environ.get(name) for name in"
        " ('MY_SETUP_VAR', 'EXTRA_VAR', 'APP_GUARD_CODEX', 'OPENCODE_CONFIG_DIR')}"
        f", open({str(seen)!r}, 'w'))"
    )
    proposer = {
        **_PLAIN,
        "env": {"MY_SETUP_VAR": "from-proposer"},
        "hidden_hashes": harness_bridge.hidden_hashes(_HIDDEN),
        "setup": [sys.executable, "-c", script],
    }
    session = tmp_path / "session"

    run_session(
        proposer,
        workspace=_git_checkout(tmp_path / "checkout"),
        prompt="x",
        model="m",
        session_dir=session,
        timeout_seconds=30,
        extra_env={"EXTRA_VAR": "extra"},
        api_key="k",
    )

    env = json.loads(seen.read_text())
    assert env["MY_SETUP_VAR"] == "from-proposer"
    assert env["EXTRA_VAR"] == "extra"
    assert env["OPENCODE_CONFIG_DIR"] == str(session / harness_bridge.GUARD_DIR)
    assert Path(env["APP_GUARD_CODEX"]).is_file()


def test_a_failed_setup_leaves_no_partial_output_and_runs_again_clean(tmp_path: Path) -> None:
    """A setup that fails removes what it wrote, so no version holds it and the next setup starts clean (SKY-753).

    Args:
        tmp_path: The checkout and the setup's log.
    """
    checkout = _git_checkout(tmp_path / "checkout")
    (checkout / "parent.txt").write_text("from the parent version")
    (checkout / ".gitignore").write_text("*.cache\n")
    log = tmp_path / "setup.log"
    write = (
        f"import os; open({str(log)!r}, 'a').write(str(os.path.exists('partial.txt')) + '\\n');"
        " os.makedirs('deps', exist_ok=True); open('deps/x', 'w'); open('partial.txt', 'w'); open('b.cache', 'w')"
    )

    with pytest.raises(RuntimeError, match="failed with exit code 3"):
        harness_bridge.ensure_setup([sys.executable, "-c", write + "; raise SystemExit(3)"], checkout)

    assert not (checkout / "partial.txt").exists()
    assert not (checkout / "deps").exists()
    assert not (checkout / "b.cache").exists()
    assert (checkout / "parent.txt").read_text() == "from the parent version"
    assert not (checkout / ".git" / harness_bridge.SETUP_STATE / "done.json").exists()

    harness_bridge.ensure_setup([sys.executable, "-c", write], checkout)

    assert log.read_text() == "False\nFalse\n"
    assert (checkout / "partial.txt").exists()


def test_a_restore_through_a_linked_parent_folder_stops_and_writes_nothing_outside(tmp_path: Path) -> None:
    """A version that turns a parent folder into a link cannot make the restore write outside the checkout (SKY-753).

    Args:
        tmp_path: The checkout and the outside folder.
    """
    checkout = tmp_path / "checkout"
    (checkout / "pkg").mkdir(parents=True)
    (checkout / "pkg" / "keep.txt").write_text("tracked")
    for arguments in (["init", "--quiet"], ["add", "--all"], ["commit", "--quiet", "--no-verify", "-m", "base"]):
        subprocess.run(
            ["git", "-c", "user.name=t", "-c", "user.email=t@example.com", *arguments], cwd=checkout, check=True
        )
    command = [sys.executable, "-c", "import os; os.makedirs('pkg/gen'); open('pkg/gen/out.txt', 'w').write('o')"]
    harness_bridge.ensure_setup(command, checkout)
    outside = tmp_path / "outside"
    outside.mkdir()
    shutil.rmtree(checkout / "pkg")
    (checkout / "pkg").symlink_to(outside)

    with pytest.raises(RuntimeError, match=r"pkg/gen cannot be restored: its parent folder .*pkg is a link"):
        harness_bridge.ensure_setup(command, checkout)

    assert list(outside.iterdir()) == []


def test_a_restore_through_a_parent_folder_linked_inside_the_checkout_stops(tmp_path: Path) -> None:
    """A parent folder that links to a folder inside the checkout also stops the restore (SKY-753).

    Args:
        tmp_path: Holds the checkout.
    """
    checkout = tmp_path / "checkout"
    (checkout / "pkg").mkdir(parents=True)
    (checkout / "pkg" / "keep.txt").write_text("tracked")
    for arguments in (["init", "--quiet"], ["add", "--all"], ["commit", "--quiet", "--no-verify", "-m", "base"]):
        subprocess.run(
            ["git", "-c", "user.name=t", "-c", "user.email=t@example.com", *arguments], cwd=checkout, check=True
        )
    command = [sys.executable, "-c", "import os; os.makedirs('pkg/gen'); open('pkg/gen/out.txt', 'w').write('o')"]
    harness_bridge.ensure_setup(command, checkout)
    other = checkout / "other"
    other.mkdir()
    shutil.rmtree(checkout / "pkg")
    (checkout / "pkg").symlink_to("other")

    with pytest.raises(RuntimeError, match=r"pkg/gen cannot be restored: its parent folder .*pkg is a link"):
        harness_bridge.ensure_setup(command, checkout)

    assert list(other.iterdir()) == []


def _git(checkout: Path, *arguments: str) -> str:
    """Run one git command in a checkout.

    Args:
        checkout: The checkout.
        *arguments: Arguments after ``git``.

    Returns:
        Its standard output, stripped.
    """
    command = ["git", "-c", "user.name=t", "-c", "user.email=t@example.com", *arguments]
    return subprocess.run(command, cwd=checkout, capture_output=True, text=True, check=True).stdout.strip()


def test_an_undo_after_a_setup_switches_branch_puts_head_and_both_branches_back(tmp_path: Path) -> None:
    """A reset of HEAD alone would move the other branch to the old commit and lose its own (SKY-753).

    Args:
        tmp_path: The checkout.
    """
    checkout = _git_checkout(tmp_path / "checkout")
    branch = _git(checkout, "symbolic-ref", "HEAD")
    head = _git(checkout, "rev-parse", "HEAD")
    _git(checkout, "checkout", "-qb", "other")
    (checkout / "o.txt").write_text("o")
    _git(checkout, "add", "o.txt")
    _git(checkout, "commit", "-qm", "o")
    other = _git(checkout, "rev-parse", "other")
    _git(checkout, "checkout", "-q", branch.removeprefix("refs/heads/"))

    with pytest.raises(RuntimeError, match="moved HEAD"):
        harness_bridge.ensure_setup(["git", "checkout", "-q", "other"], checkout)

    assert _git(checkout, "symbolic-ref", "HEAD") == branch
    assert _git(checkout, "rev-parse", "HEAD") == head
    assert _git(checkout, "rev-parse", "other") == other
    assert not (checkout / "o.txt").exists()
    assert _git(checkout, "status", "--porcelain", "--untracked-files=all") == ""


def test_an_undo_removes_a_branch_the_setup_made_and_keeps_a_detached_head(tmp_path: Path) -> None:
    """A failed setup's new branch goes, and a detached HEAD stays detached at its commit (SKY-753).

    Args:
        tmp_path: The checkout.
    """
    checkout = _git_checkout(tmp_path / "checkout")
    head = _git(checkout, "rev-parse", "HEAD")
    _git(checkout, "checkout", "-q", "--detach")
    command = ["sh", "-c", "git branch made && git checkout -q made && exit 4"]

    with pytest.raises(RuntimeError, match="failed with exit code 4"):
        harness_bridge.ensure_setup(command, checkout)

    assert _git(checkout, "for-each-ref", "refs/heads/made") == ""
    assert _git(checkout, "rev-parse", "--symbolic-full-name", "HEAD") == "HEAD"
    assert _git(checkout, "rev-parse", "HEAD") == head


def test_an_undo_after_a_setup_rewrites_gitignore_keeps_the_old_ignored_file(tmp_path: Path) -> None:
    """An emptied .gitignore unhides an old ignored file, which is not setup output and is not removed (SKY-753).

    Args:
        tmp_path: The checkout.
    """
    checkout = _git_checkout(tmp_path / "checkout")
    (checkout / ".gitignore").write_text("secret.env\nkeep/\n")
    _git(checkout, "add", ".gitignore")
    _git(checkout, "commit", "-qm", "ignore")
    (checkout / "secret.env").write_text("KEY")
    (checkout / "keep").mkdir()
    (checkout / "keep" / "old").write_text("old")
    command = ["sh", "-c", "echo > .gitignore && echo n > keep/new && exit 2"]

    with pytest.raises(RuntimeError, match="failed with exit code 2"):
        harness_bridge.ensure_setup(command, checkout)

    assert (checkout / "secret.env").read_text() == "KEY"
    assert (checkout / "keep" / "old").read_text() == "old"
    assert not (checkout / "keep" / "new").exists()
    assert (checkout / ".gitignore").read_text() == "secret.env\nkeep/\n"


def test_an_undo_after_the_exclude_lines_are_written_puts_info_exclude_back(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """A failure after the exclude lines are added removes them, byte for byte (SKY-753).

    Args:
        tmp_path: The checkout.
        monkeypatch: Makes the write of the setup's record fail.
    """
    checkout = _git_checkout(tmp_path / "checkout")
    exclude = checkout / ".git" / "info" / "exclude"
    content = exclude.read_bytes()
    write_text = Path.write_text

    def fail_the_record(self: Path, *args: Any, **kwargs: Any) -> int:
        """Fail the write of the setup's record, and write any other file.

        Args:
            self: The file to write.
            *args: The content and other arguments of :meth:`Path.write_text`.
            **kwargs: The keyword arguments of :meth:`Path.write_text`.

        Returns:
            The number of characters written, for any other file.

        Raises:
            OSError: For the setup's record.
        """
        if self.name == "done.json.tmp":
            raise OSError("disk full")
        return write_text(self, *args, **kwargs)

    monkeypatch.setattr(Path, "write_text", fail_the_record)
    with pytest.raises(OSError, match="disk full"):
        harness_bridge.ensure_setup(["sh", "-c", "mkdir build && echo x > build/f"], checkout)

    assert not (checkout / "build").exists()
    assert exclude.read_bytes() == content
    assert not (checkout / ".git" / harness_bridge.SETUP_STATE / "started.json").exists()


def test_the_next_session_undoes_a_killed_setup_and_runs_it_again(tmp_path: Path) -> None:
    """A SIGTERM skips the undo, so the next session undoes the partial setup before it trusts the checkout (SKY-753).

    Args:
        tmp_path: The checkout, the bridge config, the setup's pid file and log.
    """
    checkout = _git_checkout(tmp_path / "checkout")
    (checkout / "a.txt").write_text("the parent version's edit")
    pid_file = tmp_path / "setup.pid"
    log = tmp_path / "setup.log"
    write = (
        f"import os; open({str(log)!r}, 'a').write('ran\\n'); os.makedirs('partial'); open('partial/f', 'w').write('p')"
    )
    killed = [
        sys.executable,
        "-c",
        f"{write}; open('a.txt', 'a').write('!'); open({str(pid_file)!r}, 'w').write(str(os.getpid()))"
        "; import time; time.sleep(60)",
    ]
    config = tmp_path / "proposer.json"
    config.write_text(json.dumps({**_PLAIN, "file_command": True, "setup": killed}))
    bridge = subprocess.Popen(
        [sys.executable, str(Path(harness_bridge.__file__).resolve()), "--print", "x"],
        cwd=checkout,
        env={**os.environ, "HOME": str(tmp_path / "home"), harness_bridge.CONFIG_ENV: str(config)},
        stdout=subprocess.DEVNULL,
        stderr=subprocess.DEVNULL,
    )
    try:
        deadline = time.monotonic() + 20
        while not pid_file.exists() or not pid_file.read_text():
            assert time.monotonic() < deadline, "the setup did not start"
            time.sleep(0.05)
        bridge.send_signal(signal.SIGTERM)
        assert bridge.wait(timeout=10) == 128 + signal.SIGTERM
    finally:
        bridge.kill()
    state = checkout / ".git" / harness_bridge.SETUP_STATE
    assert (state / "started.json").exists()
    assert (checkout / "a.txt").read_text() == "the parent version's edit!"

    # The new setup fails if the partial folder is still there.
    harness_bridge.ensure_setup([sys.executable, "-c", write], checkout)

    assert log.read_text() == "ran\nran\n"
    assert (checkout / "a.txt").read_text() == "the parent version's edit"
    assert (checkout / "partial" / "f").read_text() == "p"
    assert not (state / "started.json").exists()
    assert not (state / "undo").exists()
    assert (state / "done.json").exists()


def _kill_during_setup(checkout: Path, command: list[str]) -> None:
    """Run a setup in a session that dies right after it, before its undo, as a killed session does.

    Args:
        checkout: The checkout.
        command: The setup's argument list.
    """
    script = (
        "import os, sys; from pathlib import Path; sys.path.insert(0, sys.argv[1]); import harness_bridge as bridge\n"
        "run = bridge.run_setup\n"
        "def stop(*args, **kwargs):\n"
        "    run(*args, **kwargs)\n"
        "    os._exit(143)\n"
        "bridge.run_setup = stop\n"
        f"bridge.ensure_setup({command!r}, Path(sys.argv[2]))\n"
    )
    folder = str(Path(harness_bridge.__file__).resolve().parent)
    assert subprocess.run([sys.executable, "-I", "-c", script, folder, str(checkout)], check=False).returncode == 143
    assert (checkout / ".git" / harness_bridge.SETUP_STATE / "started.json").exists()


@pytest.mark.parametrize("moved", [False, True], ids=["same-version", "engine-moved"])
def test_a_later_undo_leaves_a_checkout_that_the_engine_moved_to_another_version(
    tmp_path: Path, moved: bool, capfd: pytest.CaptureFixture[str]
) -> None:
    """GEPA's reset_tree and apply_patch between sessions make the old undo wrong for the new version (SKY-753).

    Args:
        tmp_path: The checkout.
        moved: Whether the engine moves the checkout to another version before the next session.
        capfd: Reads the bridge's note.
    """
    checkout = _git_checkout(tmp_path / "checkout")
    (checkout / "a.txt").write_text("V1")
    (checkout / "v1.txt").write_text("v1")
    _kill_during_setup(checkout, ["sh", "-c", "echo setup > a.txt && echo o > out.txt"])
    if moved:
        _git(checkout, "reset", "--quiet", "--hard", "HEAD")
        _git(checkout, "clean", "--quiet", "-fdx")
        (checkout / "a.txt").write_text("V2")
        (checkout / "v2.txt").write_text("v2")

    harness_bridge.ensure_setup(["true"], checkout)

    note = "holds another version than before the stopped setup"
    assert (note in capfd.readouterr().err) is moved
    assert (checkout / "a.txt").read_text() == ("V2" if moved else "V1")
    assert (checkout / "v2.txt").exists() is moved
    assert (checkout / "v1.txt").exists() is not moved
    assert not (checkout / "out.txt").exists()
    state = checkout / ".git" / harness_bridge.SETUP_STATE
    assert not (state / "started.json").exists()
    assert (state / "done.json").exists()


def test_a_later_undo_leaves_a_checkout_whose_head_moved_with_the_same_index(
    tmp_path: Path, capfd: pytest.CaptureFixture[str]
) -> None:
    """A move of HEAD alone, with no index write, also means another version, so the undo keeps the files (SKY-753).

    Args:
        tmp_path: The checkout.
        capfd: Reads the bridge's note.
    """
    checkout = _git_checkout(tmp_path / "checkout")
    _kill_during_setup(checkout, ["sh", "-c", "echo setup > a.txt && echo o > out.txt"])
    state = checkout / ".git" / harness_bridge.SETUP_STATE
    started = json.loads((state / "started.json").read_text())
    other = _git(checkout, "commit-tree", "HEAD^{tree}", "-p", "HEAD", "-m", "other")
    _git(checkout, "update-ref", "--no-deref", "HEAD", other)
    # The test is only about the HEAD half of the check when the index file is still the one from before the setup.
    assert harness_bridge._index_stamp(checkout) == started["index_stamp"]

    harness_bridge.ensure_setup(["true"], checkout)

    assert "holds another version than before the stopped setup" in capfd.readouterr().err
    assert (checkout / "a.txt").read_text() == "setup\n"
    assert (checkout / "out.txt").read_text() == "o\n"
    assert not (state / "started.json").exists()
    assert (state / "done.json").exists()


@pytest.mark.parametrize("lock_before", [False, True], ids=["left-by-setup", "there-before"])
def test_a_later_undo_removes_only_an_index_lock_that_the_stopped_setup_left(tmp_path: Path, lock_before: bool) -> None:
    """A setup killed inside a git command leaves index.lock, which would make every later undo fail (SKY-753).

    Args:
        tmp_path: The checkout.
        lock_before: Whether the lock file was there before the setup.
    """
    checkout = _git_checkout(tmp_path / "checkout")
    lock = checkout / ".git" / "index.lock"
    if lock_before:
        lock.write_text("")
    _kill_during_setup(checkout, ["sh", "-c", "echo setup > a.txt && touch .git/index.lock"])

    if lock_before:
        with pytest.raises(RuntimeError, match=r"git index lock .*index\.lock was there before the setup"):
            harness_bridge.ensure_setup(["true"], checkout)
        assert lock.exists()
        assert (checkout / ".git" / harness_bridge.SETUP_STATE / "started.json").exists()
        return
    harness_bridge.ensure_setup(["true"], checkout)

    assert not lock.exists()
    assert (checkout / "a.txt").read_text() == "tracked"
    assert _git(checkout, "status", "--porcelain", "--untracked-files=all") == ""


def test_an_undo_of_many_changed_files_stays_under_the_argument_limit(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """One git argument per changed file would pass the system's argument limit in a large repository (SKY-753).

    Args:
        tmp_path: The checkout.
        monkeypatch: Makes the argument limit small.
    """
    checkout = _git_checkout(tmp_path / "checkout")
    names = [f"file-{number:03d}-{'x' * 40}.txt" for number in range(80)]
    for name in names:
        (checkout / name).write_text("old")
    _git(checkout, "add", "--all")
    _git(checkout, "commit", "--quiet", "--no-verify", "-m", "many")
    run = subprocess.run
    checkouts: list[list[str]] = []

    def small_limit(arguments: list[str], *args: Any, **kwargs: Any) -> subprocess.CompletedProcess[Any]:
        """Fail like the system does for a long argument list, and run any other command.

        Args:
            arguments: The command.
            *args: The other arguments of :func:`subprocess.run`.
            **kwargs: The keyword arguments of :func:`subprocess.run`.

        Returns:
            The finished command.

        Raises:
            OSError: When the arguments are longer than 1000 bytes.
        """
        if sum(len(argument) + 1 for argument in arguments) > 1000:
            raise OSError(7, "Argument list too long")
        if arguments[:4] == ["git", *harness_bridge._SAFE_GIT, "checkout"]:
            checkouts.append(arguments)
        return run(arguments, *args, **kwargs)

    monkeypatch.setattr(harness_bridge.subprocess, "run", small_limit)
    with pytest.raises(RuntimeError, match="changed tracked files") as failure:
        harness_bridge.ensure_setup(["sh", "-c", "for f in file-*; do echo new > $f; done"], checkout)

    assert not getattr(failure.value, "__notes__", [])
    # One process for all the files, not one for each.
    assert len(checkouts) == 1
    assert all((checkout / name).read_text() == "old" for name in names)
    assert not (checkout / ".git" / harness_bridge.SETUP_STATE / "started.json").exists()


@pytest.mark.parametrize("ignored", [False, True], ids=["untracked", "ignored"])
def test_a_failed_setup_keeps_an_empty_folder_that_was_there_before(tmp_path: Path, ignored: bool) -> None:
    """An empty folder has no old file under it, but it is not the setup's output either (SKY-753).

    Args:
        tmp_path: The checkout.
        ignored: Whether git ignores the folder.
    """
    checkout = _git_checkout(tmp_path / "checkout")
    if ignored:
        (checkout / ".git" / "info" / "exclude").write_text("cache/\n")
    (checkout / "cache" / "inner").mkdir(parents=True)

    with pytest.raises(RuntimeError, match="failed with exit code 3"):
        harness_bridge.ensure_setup(
            ["sh", "-c", "touch cache/x cache/inner/y && mkdir new && touch new/z && exit 3"], checkout
        )

    assert (checkout / "cache" / "inner").is_dir()
    assert sorted(path.name for path in (checkout / "cache").rglob("*")) == ["inner"]
    assert not (checkout / "new").exists()


def test_a_setup_output_in_an_old_empty_folder_excludes_its_files_and_not_the_folder(tmp_path: Path) -> None:
    """An exclude line for the whole old folder would hide what a version later writes into it (SKY-753).

    Args:
        tmp_path: The checkout.
    """
    checkout = _git_checkout(tmp_path / "checkout")
    (checkout / "cache").mkdir()
    (checkout / "empty").mkdir()

    harness_bridge.ensure_setup(["sh", "-c", "touch cache/x && mkdir new && touch new/y"], checkout)

    state = checkout / ".git" / harness_bridge.SETUP_STATE
    assert json.loads((state / "done.json").read_text())["output"] == ["cache/x", "new/"]
    lines = (checkout / ".git" / "info" / "exclude").read_text().splitlines()
    assert "/cache/x" in lines
    assert "/new/" in lines
    assert not {"/cache/", "/empty/"} & set(lines)


def test_the_copy_of_the_checkout_counts_toward_the_setup_time_limit(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """A slow copy before the setup uses up the limit, so the setup does not start late (SKY-753).

    Args:
        tmp_path: The checkout and the setup's log.
        monkeypatch: Makes the copy slow.
    """
    checkout = _git_checkout(tmp_path / "checkout")
    log = tmp_path / "setup.log"
    save = harness_bridge._save_checkout

    def slow_save(*args: Any) -> None:
        """Copy the checkout, and take longer than the whole time limit.

        Args:
            *args: The arguments of :func:`harness_bridge._save_checkout`.
        """
        save(*args)
        time.sleep(0.6)

    monkeypatch.setattr(harness_bridge, "_save_checkout", slow_save)
    with pytest.raises(RuntimeError, match=r"no time was left within 0\.5 seconds"):
        harness_bridge.ensure_setup([sys.executable, "-c", f"open({str(log)!r}, 'w')"], checkout, timeout_seconds=0.5)

    assert not log.exists()
    assert not (checkout / ".git" / harness_bridge.SETUP_STATE / "started.json").exists()
    assert not (checkout / ".git" / harness_bridge.SETUP_STATE / "undo").exists()


def test_checkout_git_refuses_a_bare_repository_layout(
    planted_bare_repo: tuple[Path, Path], monkeypatch: pytest.MonkeyPatch
) -> None:
    """The setup undo's git helper does not treat a bare-repository layout as a repository (SKY-812).

    Args:
        planted_bare_repo: Fixture.
        monkeypatch: Pytest fixture.
    """
    assert harness_bridge._SAFE_GIT == repo_tree.SAFE_GIT
    repo, _ = planted_bare_repo
    with pytest.raises(RuntimeError, match=r"safe\.bareRepository"):
        harness_bridge._checkout_git(["rev-parse", "--git-dir"], repo)
    monkeypatch.setattr(harness_bridge, "_SAFE_GIT", ())
    assert harness_bridge._checkout_git(["rev-parse", "--git-dir"], repo).strip() == "."


# A built-in agent's model calls: one chat request for each name, from the route key the bridge fills in.
_AGENT_PROBE = """
import json, os, sys, urllib.error, urllib.request

key, record, *models = sys.argv[1:]
statuses = {}
for model in models:
    request = urllib.request.Request(
        os.environ["OPENAI_BASE_URL"] + "/chat/completions",
        data=json.dumps({"model": model, "messages": [{"role": "user", "content": "go"}]}).encode(),
        headers={"Authorization": "Bearer " + key, "Content-Type": "application/json"},
    )
    try:
        with urllib.request.urlopen(request, timeout=20) as response:
            statuses[model] = [response.status, ""]
    except urllib.error.HTTPError as error:
        statuses[model] = [error.code, json.loads(error.read())["error"]["message"]]
open(record, "w").write(json.dumps(statuses))
print("done")
"""
# What a built-in agent sends: the exact ASCENT_MODEL, LiteLLM's openai/ form (Aider, OpenHands, SWE-agent,
# mini-SWE-agent, Terminus, OpenHands SDK), the harness provider's app/ form (OpenCode) and a transport prefix.
_AGENT_NAMES = ("run-model", "openai/run-model", "app/run-model", "litellm_proxy/run-model")


@pytest.fixture
def bindings(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Path:
    """Give a non-root box proxy a model bindings folder and no ``ASCENT_MODEL`` of its own.

    Args:
        tmp_path: Holds the bindings folder.
        monkeypatch: Sets the folder, removes ``ASCENT_MODEL`` and makes the proxy a non-root user.

    Returns:
        The bindings folder.
    """
    folder = tmp_path / "models"
    folder.mkdir()
    monkeypatch.delenv(guest_model_proxy.MODEL_ENV, raising=False)
    monkeypatch.setenv(guest_model_proxy.MODEL_BINDINGS_ENV, str(folder))
    monkeypatch.setattr(guest_model_proxy.os, "geteuid", lambda: 1000, raising=False)
    return folder


# The test cannot change the proxy subprocess's user, and a root proxy reads no bindings.
@pytest.mark.skipif(getattr(os, "geteuid", lambda: 1)() == 0, reason="a root box proxy reads no model bindings")
def test_box_proxy_refuses_another_model_in_an_agent_session_and_names_ascent_model(tmp_path: Path) -> None:
    """A session's request for another model gets the box error that names ``ASCENT_MODEL`` (SKY-884).

    The box proxy has no ``ASCENT_MODEL`` of its own, as in the harness proposer box. The exact
    model and each prefixed form that a built-in agent sends reach the supervisor.

    Args:
        tmp_path: Folder for the proxy's mailbox, the session and the probe's record.
    """
    home = tmp_path / "home"
    workspace = tmp_path / "work"
    workspace.mkdir()
    probe = tmp_path / "probe.py"
    probe.write_text(_AGENT_PROBE)
    record = tmp_path / "record.json"
    models = ["other/model", *_AGENT_NAMES]
    run_command = shlex.join([sys.executable, str(probe), harness_bridge.KEY_TOKEN, str(record), *models])
    config = tmp_path / "proposer.json"
    config.write_text(json.dumps({**_PLAIN, "run_command": run_command}))
    responses = tmp_path / "box" / "responses"
    proxy_config = tmp_path / "proxy.json"
    proxy_config.write_text(json.dumps({"prefix": "P:", "response_dir": str(responses), "timeout_seconds": 20}))
    env = {
        **{name: value for name, value in os.environ.items() if name != guest_model_proxy.MODEL_ENV},
        "HOME": str(home),
        harness_bridge.CONFIG_ENV: str(config),
        harness_bridge.KEY_ENV: "route-key",
    }
    bridge = [sys.executable, harness_bridge.__file__, "--print", "task", "--output-format", "json"]
    proxy = subprocess.Popen(
        [sys.executable, guest_model_proxy.__file__, str(proxy_config), *bridge, "--model", "run-model"],
        cwd=workspace,
        stdout=subprocess.PIPE,
        text=True,
        env=env,
    )
    relayed = []
    # A hung proxy closes its output when killed, so the loop ends and the test fails instead of blocking.
    watchdog = threading.Timer(60, proxy.kill)
    watchdog.start()
    try:
        assert proxy.stdout is not None
        for line in proxy.stdout:
            if line.startswith("P:"):
                request = json.loads(line[2:])
                relayed.append(json.loads(base64.b64decode(request["body"]))["model"])
                answer = {"status": 200, "content_type": "application/json", "body": base64.b64encode(b"{}").decode()}
                staged = responses / f"{request['id']}.tmp"
                staged.write_text(json.dumps(answer))
                staged.rename(responses / f"{request['id']}.json")
        assert proxy.wait(timeout=60) == 0
    finally:
        watchdog.cancel()
        proxy.kill()

    statuses = json.loads(record.read_text())
    status, message = statuses.pop("other/model")
    assert status == 400
    assert "ASCENT_MODEL" in message
    assert "run-model" in message
    assert statuses == {name: [200, ""] for name in _AGENT_NAMES}
    assert relayed == list(_AGENT_NAMES)


@pytest.mark.parametrize("model", _AGENT_NAMES)
def test_box_proxy_accepts_each_name_a_built_in_agent_sends(bindings: Path, model: str) -> None:
    """The exact session model and each prefixed form of it pass the box check (SKY-884).

    Args:
        bindings: The model bindings folder.
        model: A name of the session's model.
    """
    harness_bridge.bind_model(bindings, "route-key", "run-model")
    body = json.dumps({"model": model}).encode()

    assert guest_model_proxy._other_model_refusal("/v1/chat/completions", body, "route-key") is None


@pytest.mark.parametrize("model", ["other/model", "openai/other", None])
def test_box_proxy_refuses_another_model_for_a_bound_key(bindings: Path, model: str | None) -> None:
    """A request for another model with a session's key gets the error that names ``ASCENT_MODEL`` (SKY-884).

    Args:
        bindings: The model bindings folder.
        model: Another model's name, or no model.
    """
    harness_bridge.bind_model(bindings, "route-key", "run-model")
    body = json.dumps({"model": model}).encode()

    refusal = guest_model_proxy._other_model_refusal("/v1/chat/completions", body, "route-key")

    assert refusal is not None
    error = json.loads(refusal)["error"]
    assert error["code"] == "model_not_allowed"
    assert "ASCENT_MODEL" in error["message"]


def test_box_proxy_lets_a_key_with_no_session_model_go_on(bindings: Path) -> None:
    """A key that no session named a model for is left to the gateway, and the folder holds no key (SKY-884).

    Args:
        bindings: The model bindings folder.
    """
    harness_bridge.bind_model(bindings, "route-key", "run-model")
    body = json.dumps({"model": "other/model"}).encode()

    assert guest_model_proxy._other_model_refusal("/v1/chat/completions", body, "another-key") is None
    assert guest_model_proxy._other_model_refusal("/v1/chat/completions", body) is None
    assert [path.name for path in bindings.iterdir()] == [guest_model_proxy.key_digest("route-key")]
    assert "route-key" not in "".join(path.read_text() for path in bindings.rglob("*") if path.is_file())


@pytest.mark.parametrize("path", ["/v1/responses", "/v1/messages"])
def test_box_proxy_checks_the_responses_and_messages_routes(bindings: Path, path: str) -> None:
    """Codex's Responses requests and Anthropic messages get the same model check as chat (SKY-884).

    Args:
        bindings: The model bindings folder.
        path: A model route that names its model at the top level of the body.
    """
    harness_bridge.bind_model(bindings, "route-key", "run-model")

    refusal = guest_model_proxy._other_model_refusal(path, json.dumps({"model": "other/model"}).encode(), "route-key")

    assert refusal is not None
    assert "ASCENT_MODEL" in json.loads(refusal)["error"]["message"]
    accepted = json.dumps({"model": "openai/run-model"}).encode()
    assert guest_model_proxy._other_model_refusal(path, accepted, "route-key") is None


def _refusal_message(key: str = "route-key") -> str:
    """Return the box error for a request for another model with a key.

    Args:
        key: The request's route key.

    Returns:
        The error message.
    """
    refusal = guest_model_proxy._other_model_refusal(
        "/v1/chat/completions", json.dumps({"model": "other/model"}).encode(), key
    )
    assert refusal is not None
    return json.loads(refusal)["error"]["message"]


def test_box_proxy_ignores_a_linked_binding_file(bindings: Path, tmp_path: Path) -> None:
    """A link in a key folder never puts the linked file's text in the error.

    Args:
        bindings: The model bindings folder.
        tmp_path: Holds the secret that the link points at.
    """
    harness_bridge.bind_model(bindings, "route-key", "run-model")
    secret = tmp_path / "environ"
    secret.write_text("APP_MCP_RELAY_TOKEN=secret-value")
    (bindings / guest_model_proxy.key_digest("route-key") / "linked").symlink_to(secret)

    message = _refusal_message()

    assert "secret-value" not in message
    assert "run-model" in message


def test_box_proxy_ignores_a_linked_key_folder(bindings: Path, tmp_path: Path) -> None:
    """A key folder that is a link is not read, so the request goes on to the gateway.

    Args:
        bindings: The model bindings folder.
        tmp_path: Holds the folder that the link points at.
    """
    elsewhere = tmp_path / "elsewhere"
    elsewhere.mkdir()
    (elsewhere / "name").write_text("secret-value")
    (bindings / guest_model_proxy.key_digest("route-key")).symlink_to(elsewhere, target_is_directory=True)
    body = json.dumps({"model": "other/model"}).encode()

    assert guest_model_proxy._other_model_refusal("/v1/chat/completions", body, "route-key") is None


@pytest.mark.parametrize("text", ["x" * 300, "run-model\nAPP_MCP_RELAY_TOKEN=secret-value", ""])
def test_box_proxy_ignores_a_long_or_multi_line_binding(bindings: Path, text: str) -> None:
    """A binding that is not one short printable line is skipped, and its text is not in the error.

    Args:
        bindings: The model bindings folder.
        text: An oversized, multi-line or empty binding.
    """
    harness_bridge.bind_model(bindings, "route-key", "run-model")
    (bindings / guest_model_proxy.key_digest("route-key") / "bad").write_text(text)

    message = _refusal_message()

    assert "secret-value" not in message
    assert "x" * 300 not in message
    assert "(run-model)" in message


def test_box_proxy_ignores_a_fifo_binding(bindings: Path) -> None:
    """A FIFO in a key folder neither blocks the handler nor counts as a model.

    Args:
        bindings: The model bindings folder.
    """
    harness_bridge.bind_model(bindings, "route-key", "run-model")
    os.mkfifo(bindings / guest_model_proxy.key_digest("route-key") / "fifo")

    assert "(run-model)" in _refusal_message()


def test_root_box_proxy_reads_no_bindings(bindings: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    """A root proxy, as the scorer's supervisor runs it, reads no model bindings.

    Args:
        bindings: The model bindings folder.
        monkeypatch: Makes the proxy root.
    """
    harness_bridge.bind_model(bindings, "route-key", "run-model")
    monkeypatch.setattr(guest_model_proxy.os, "geteuid", lambda: 0, raising=False)
    body = json.dumps({"model": "other/model"}).encode()

    assert guest_model_proxy._bound_models("route-key") == []
    assert guest_model_proxy._other_model_refusal("/v1/chat/completions", body, "route-key") is None
