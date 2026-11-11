"""Tests for the ``claude``-compatible bridge that drives any harness for the upstream engines."""

from __future__ import annotations

import contextlib
import hashlib
import http.server
import json
import os
import shlex
import shutil
import signal
import subprocess
import sys
import threading
import time
from pathlib import Path
from types import SimpleNamespace
from typing import Any

import pytest

from .. import harness, harness_bridge
from ..harness_bridge import (
    SessionOutcome,
    _cli_parser,
    install_shim,
    record_transcript,
    result_document,
    run_session,
)

_PLAIN = {"run_command": 'cat "$APP_PROMPT_FILE"', "output_format": "plain", "files": {}, "env": {}}


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
    """Materialize the launch in the workspace, swap in the real key and capture the harness answer."""
    proposer = {
        **_PLAIN,
        "run_command": 'cat "$APP_PROMPT_FILE"; cat config.json; echo "$TOKEN"',
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


def test_run_session_accepts_the_legacy_key_token_and_variables(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Restore a saved launch's pre-rename key token and expose both variable spellings."""
    proposer = {
        **_PLAIN,
        "run_command": 'echo "$APP_MODEL $SKYNET_MODEL"; cat config.json',
        "files": {"config.json": "__SKYNET_API_KEY__"},
    }
    monkeypatch.delenv(harness_bridge.KEY_ENV, raising=False)
    monkeypatch.setenv("SKYNET_API_KEY", "legacy-key")
    outcome = run_session(
        proposer, workspace=tmp_path / "work", prompt="hi", model="m1", session_dir=tmp_path / "session"
    )
    assert outcome.returncode == 0
    assert outcome.text == "m1 m1\nlegacy-key"


def test_run_session_reports_timeouts_and_budget_overruns(tmp_path: Path) -> None:
    """Kill a harness that outlives its timeout and stop one whose priced usage exceeds the budget."""
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
    """Signal the shell's group once, then only the descendants that left it, never a group member twice."""
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
    """Send nothing when the reaped shell's pid now belongs to a process with another start time."""
    sent = _record_signals(monkeypatch, {100: (1, 100, 50), 101: (100, 101, 51)})

    harness_bridge._stop_group(SimpleNamespace(pid=100), signal.SIGKILL, 7)  # type: ignore[arg-type]

    assert sent == []


def test_stop_group_signals_the_group_when_the_reaped_pid_is_free(monkeypatch: pytest.MonkeyPatch) -> None:
    """Signal only the group when the shell is gone and nothing took its pid, as members can still hold the pipe."""
    sent = _record_signals(monkeypatch, {101: (1, 100, 8)})

    harness_bridge._stop_group(SimpleNamespace(pid=100), signal.SIGKILL, 7)  # type: ignore[arg-type]

    assert sent == [("group", 100, signal.SIGKILL)]


@pytest.mark.skipif(not Path("/proc/self/stat").exists(), reason="the descendant sweep reads /proc")
def test_stop_group_kills_a_descendant_in_its_own_session(tmp_path: Path) -> None:
    """Kill a grandchild that left the harness group and session, the way eve's sandbox starts a tool command."""
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
    """Kill an orphaned ``sleep`` whose detached parent already exited, the way ``sleep 300 &`` in an eve tool does."""
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
    """Kill an orphaned ``sleep`` from a detached tool when the harness ends on its own, with no stop."""
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


def test_normal_end_kills_a_background_job_left_in_the_harness_group(tmp_path: Path) -> None:
    """Kill a ``sleep &`` that the harness shell left in its own group when the shell ends on its own."""
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
    """Keep SIGPIPE at its default in the harness shell, so ``producer | head -1`` ends at once."""
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
    """Let the shell's EXIT trap finish after a tool-call cap stop, so a large temp folder is gone."""
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
    except FileNotFoundError:
        return False


def test_result_document_and_transcript_match_what_upstream_reads(tmp_path: Path) -> None:
    """Emit a Claude-shaped result and a per-project transcript that ``collect_usage`` can price."""
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
    """Replay the first task before the follow-up nudge because other harnesses cannot resume a session id."""
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
    """Give the harness a closed stdin so a CLI that reads it never waits on the bridge's own input."""
    proposer = {**_PLAIN, "run_command": 'if read -r line; then echo "got:$line"; else echo eof; fi'}
    workspace = tmp_path / "w"
    workspace.mkdir()
    outcome = run_session(
        proposer, workspace=workspace, prompt="x", model="m1", session_dir=tmp_path / "s", timeout_seconds=10
    )
    assert outcome.stdout.strip() == "eof"


def test_shim_reports_why_the_harness_failed_on_stderr(tmp_path: Path) -> None:
    """Surface the harness's own error even when its stdout ends with an ordinary chat message."""
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
    """Stop a harness once it starts more tool calls than the cap, without calling the session failed."""
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
    """Let a harness run to its end when the launch names no cap."""
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
    """Count a Codex item announced on start and settled on completion as one call."""
    item = {"id": "i1", "type": "command_execution"}
    events = [{"type": "item.started", "item": item}, {"type": "item.completed", "item": item}]
    command = "; ".join(f"echo '{json.dumps(event)}'" for event in events)
    proposer = {**_PLAIN, "output_format": "codex", "run_command": command, "max_tool_calls": 5}
    outcome = run_session(proposer, workspace=tmp_path / "w", prompt="x", model="m", session_dir=tmp_path / "s")
    assert outcome.tool_calls == 1
    assert outcome.tool_cap_reached is False


# The worker's hidden list lands with SKY-386 and SKY-387; these tests use a fixed one.
_HIDDEN = [
    "optimize.toml",
    "metric",
    ".optimize/exploits",
    "reference/answers.json",
    "data/cases.jsonl",
    "rules/illegal.md",
    "tools/mcp_server.py",
]
_HIDDEN_FILES = ["optimize.toml", "metric/score.py", ".optimize/exploits/log.md", *_HIDDEN[3:]]
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
    """Build a guard policy over the fixed hidden list."""
    workspace = tmp_path / "work"
    workspace.mkdir(exist_ok=True)
    return harness_bridge.guard_policy(workspace, _HIDDEN, tmp_path / "refusals.jsonl")


@pytest.mark.parametrize("path", _HIDDEN_FILES)
@pytest.mark.parametrize(("tool", "shape"), _TOOL_CALLS, ids=[tool for tool, _ in _TOOL_CALLS])
def test_guard_refuses_every_hidden_path_by_exact_name_in_every_tool(tmp_path: Path, tool, shape, path) -> None:
    """Refuse a read, grep, glob, list or shell call on each hidden path, with the right kind."""
    policy = _policy(tmp_path)
    expected = "config" if path == "optimize.toml" else "scoring"
    assert harness_bridge.guard_check(policy, tool, shape(path)) == expected
    absolute = f"{policy['workspace']}/{path}"
    assert harness_bridge.guard_check(policy, tool, shape(absolute)) == expected


@pytest.mark.parametrize(
    ("tool", "tool_input"),
    [
        ("Glob", {"pattern": "**/*"}),
        ("Bash", {"command": "ls -R ."}),
        ("Grep", {"pattern": "optimize.toml", "path": "src"}),
        ("Read", {"file_path": "src/optimize.toml.md"}),
        ("Read", {"file_path": "metrics/score.py"}),
        ("Read", {"file_path": "../work-other/optimize.toml"}),
    ],
)
def test_guard_lets_other_paths_and_broad_patterns_run(tmp_path: Path, tool: str, tool_input: dict) -> None:
    """Match exact hidden names only, so broad globs and look-alike names still work."""
    assert harness_bridge.guard_check(_policy(tmp_path), tool, tool_input) is None


def test_guard_policy_holds_no_hidden_names(tmp_path: Path) -> None:
    """Keep the hidden names out of the policy file the agent's workspace can read."""
    text = json.dumps(_policy(tmp_path))
    for path in _HIDDEN:
        assert path not in text


@pytest.mark.parametrize(
    ("tool", "tool_input"),
    [
        ("Read", {"file_path": "alias/optimize.toml"}),
        ("Bash", {"command": "cd src && cat ../optimize.toml"}),
        ("Bash", {"command": "cd src; cd deep && cat ../../metric/score.py"}),
        ("exec_command", {"cmd": "cat ../optimize.toml", "workdir": "src"}),
        ("Bash", {"command": 'cat "$(pwd)/optimize.toml"'}),
        ("Bash", {"command": "cat $(pwd)/optimize.toml"}),
        ("Bash", {"command": "cat `pwd`/data/cases.jsonl"}),
        ("Read", {"file_path": "${PWD}/optimize.toml"}),
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
    """Refuse a hidden path that a symbolic link, a cd, a workdir or a pwd form names."""
    policy = _policy(tmp_path)
    (Path(policy["workspace"]) / "alias").symlink_to(".")
    assert harness_bridge.guard_check(policy, tool, tool_input) is not None


def test_guard_folds_case_only_when_the_policy_says_so(tmp_path: Path) -> None:
    """Refuse OPTIMIZE.TOML on a case-folding policy, and only there."""
    workspace = tmp_path / "work"
    call = {"file_path": "OPTIMIZE.TOML"}
    folded = harness_bridge.guard_policy(workspace, _HIDDEN, tmp_path / "log", fold_case=True)
    exact = harness_bridge.guard_policy(workspace, _HIDDEN, tmp_path / "log")
    assert harness_bridge.guard_check(folded, "Read", call) == "config"
    assert harness_bridge.guard_check(folded, "Read", {"file_path": "Metric/Score.py"}) == "scoring"
    assert harness_bridge.guard_check(exact, "Read", call) is None


@pytest.mark.parametrize(
    ("tool", "tool_input"),
    [
        ("Bash", {"command": "cat ~/shinka-run/repo/optimize.toml"}),
        ("Bash", {"command": "cat $HOME/shinka-run/repo/optimize.toml"}),
        ("Bash", {"command": "cat ${HOME}/shinka-run/repo/metric/score.py"}),
        ("Bash", {"command": "cd ~/shinka-run/repo && cat optimize.toml"}),
        ("Bash", {"command": "cd ~ && cat shinka-run/repo/data/cases.jsonl"}),
        ("Read", {"file_path": "~/shinka-run/repo/optimize.toml"}),
        ("exec_command", {"cmd": "cat optimize.toml", "workdir": "~/shinka-run/repo"}),
    ],
    ids=["tilde", "home", "brace-home", "cd-tilde", "cd-bare-tilde", "read-tilde", "codex-workdir-tilde"],
)
def test_guard_expands_the_home_folder_forms(tmp_path: Path, tool: str, tool_input: dict) -> None:
    """Refuse a hidden path that ``~``, ``$HOME`` or ``${HOME}`` names, as in a ShinkaEvolve session."""
    home = tmp_path / "home"
    workspace = home / "shinka-run" / "repo"
    workspace.mkdir(parents=True)
    policy = harness_bridge.guard_policy(workspace, _HIDDEN, tmp_path / "log", home=home)
    assert policy["home"] == str(home)
    assert harness_bridge.guard_check(policy, tool, tool_input) is not None


@pytest.mark.parametrize(
    "command",
    ["cat x~/shinka-run/repo/optimize.toml", "cat ~/optimize.toml"],
)
def test_guard_expands_a_tilde_only_at_the_start_of_a_word(tmp_path: Path, command: str) -> None:
    """Leave a tilde inside a word, and a home path outside the workspace, alone."""
    home = tmp_path / "home"
    workspace = home / "shinka-run" / "repo"
    workspace.mkdir(parents=True)
    policy = harness_bridge.guard_policy(workspace, _HIDDEN, tmp_path / "log", home=home)
    assert harness_bridge.guard_check(policy, "Bash", {"command": command}) is None


@pytest.mark.parametrize(
    "command",
    [
        "cat ~+/optimize.toml",
        "cd metric && cat ~-/optimize.toml",
        "cat $(pwd -P)/optimize.toml",
        "cat $(pwd -L)/optimize.toml",
        "cat ${PWD:-x}/optimize.toml",
        "cat ${HOME:-x}/shinka-run/repo/optimize.toml",
        "cat ${HOME%/}/shinka-run/repo/optimize.toml",
        "cd metric; cat $OLDPWD/optimize.toml",
        "cat ~agent/shinka-run/repo/optimize.toml",
        "cat $HOMEDIR/shinka-run/repo/optimize.toml",
        "cat `pwd -P`/metric/score.py",
        'cat "$(dirname x)"/data/cases.jsonl',
    ],
)
def test_guard_refuses_a_hidden_name_after_an_unresolved_expansion(tmp_path: Path, command: str) -> None:
    """Refuse a hidden name after any shell expansion the guard cannot resolve to a known folder."""
    home = tmp_path / "home"
    workspace = home / "shinka-run" / "repo"
    workspace.mkdir(parents=True)
    policy = harness_bridge.guard_policy(workspace, _HIDDEN, tmp_path / "log", home=home)
    assert harness_bridge.guard_check(policy, "Bash", {"command": command}) is not None


@pytest.mark.parametrize(
    "command",
    [
        "cd src && python $(pwd -P)/solve.py --out ${OUT:-out}/result.json",
        "echo $PATH ~+ ~- `date` && cat src/optimize.toml.md",
        "cat ${HOME:-x}/notes/metrics.md",
    ],
)
def test_guard_lets_an_expansion_without_a_hidden_name_run(tmp_path: Path, command: str) -> None:
    """Let a normal command run when no hidden name follows its shell expansions."""
    home = tmp_path / "home"
    workspace = home / "shinka-run" / "repo"
    workspace.mkdir(parents=True)
    policy = harness_bridge.guard_policy(workspace, _HIDDEN, tmp_path / "log", home=home)
    assert harness_bridge.guard_check(policy, "Bash", {"command": command}) is None


def test_guard_policy_home_defaults_to_this_process_home(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    """Record this process's home folder when the caller names none, so the digest covers it."""
    monkeypatch.setenv("HOME", str(tmp_path / "agent-home"))
    assert _policy(tmp_path)["home"] == str(tmp_path / "agent-home")


def test_guard_folds_the_case_of_the_workspace_root(tmp_path: Path) -> None:
    """Refuse an absolute hidden path whose root folder has another case, on a case-folding policy."""
    workspace = tmp_path / "work"
    workspace.mkdir()
    folded = harness_bridge.guard_policy(workspace, _HIDDEN, tmp_path / "log", fold_case=True)
    first, rest = str(workspace).lstrip("/").split("/", 1)
    call = {"file_path": f"/{first.upper()}/{rest}/optimize.toml"}
    assert harness_bridge.guard_check(folded, "Read", call) == "config"
    assert harness_bridge.guard_check(_policy(tmp_path), "Read", call) is None


def _digest(policy_file: Path) -> str:
    """Hash the policy file as the guard checks it."""
    return hashlib.sha256(policy_file.read_bytes()).hexdigest()


def _guard(mode: str, policy_file: Path, request: dict) -> dict:
    """Run the guard CLI once and parse its answer."""
    completed = subprocess.run(
        harness_bridge.guard_command(mode, policy_file, _digest(policy_file)),
        input=json.dumps(request),
        capture_output=True,
        text=True,
        check=True,
    )
    return json.loads(completed.stdout or "{}")


def test_guard_cli_answers_codex_hooks_and_js_checks_and_logs_only_tool_and_kind(tmp_path: Path) -> None:
    """Deny in the Codex hook shape, answer the JS check shape, and log no path or content."""
    session = tmp_path / "session"
    env = harness_bridge.install_guard(session, tmp_path / "work", _HIDDEN)
    policy_file = session / harness_bridge.GUARD_DIR / "policy.json"
    denied = _guard("hook", policy_file, {"tool_name": "exec_command", "tool_input": {"cmd": "cat optimize.toml"}})
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
    """Skip a line that is not JSON, too deep to parse, a non-object, or without a string tool and kind."""
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
    """Refuse a call with more cd or pushd steps than the guard follows, in the check and the CLI."""
    session = tmp_path / "session"
    harness_bridge.install_guard(session, tmp_path / "work", _HIDDEN)
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
    """Refuse a hidden name behind a ``cd`` option or a folder option such as ``env -C``."""
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
    """Allow a context count after ``grep -C`` and a ``cd`` option before an ordinary folder."""
    assert harness_bridge.guard_check(_policy(tmp_path), "Bash", {"command": command}) is None


@pytest.mark.parametrize(
    "command", ["cd; cat shinka-run/repo/optimize.toml", "cd && cat shinka-run/repo/optimize.toml"]
)
def test_guard_follows_a_bare_cd_to_the_home_folder(tmp_path: Path, command: str) -> None:
    """Refuse a hidden path after a ``cd`` with no folder, which goes to the home folder."""
    home = tmp_path / "home"
    workspace = home / "shinka-run" / "repo"
    workspace.mkdir(parents=True)
    policy = harness_bridge.guard_policy(workspace, _HIDDEN, tmp_path / "log", home=home)
    assert harness_bridge.guard_check(policy, "Bash", {"command": command}) == "config"
    assert harness_bridge.guard_check(policy, "Bash", {"command": "cd; cat notes/optimize.md"}) is None


def test_guard_checks_a_call_in_the_folder_the_hook_event_names(tmp_path: Path) -> None:
    """Refuse a hidden name relative to the event's ``cwd``, as after an earlier ``cd data`` call."""
    session = tmp_path / "session"
    harness_bridge.install_guard(session, tmp_path / "work", _HIDDEN)
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
        """Return a target of ``budget`` characters that ends where it starts."""
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
    """Deny a call whose links point at long paths, well before the 10 s hook timeout."""
    session = tmp_path / "session"
    workspace = tmp_path / "work"
    workspace.mkdir()
    harness_bridge.install_guard(session, workspace, _HIDDEN)
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
    """
    session = tmp_path / "session"
    harness_bridge.install_guard(session, tmp_path / "work", _HIDDEN)
    policy_file = session / harness_bridge.GUARD_DIR / "policy.json"
    workspace = tmp_path / "work"
    workspace.mkdir(exist_ok=True)
    for index in range(900):
        (workspace / f"c{index}").symlink_to(f"c{index + 1}")
    for index in range(3000):
        (workspace / f"e{index}").symlink_to("c0")
    command = "cat " + " ".join(f"e{index}" for index in range(3000)) + " optimize.toml"
    with pytest.raises(ValueError, match="symbolic links"):
        harness_bridge.guard_check(json.loads(policy_file.read_text()), "Bash", {"command": command})
    started = time.monotonic()
    denied = _guard("hook", policy_file, {"tool_name": "Bash", "tool_input": {"command": command}})
    assert denied["hookSpecificOutput"]["permissionDecision"] == "deny"
    assert time.monotonic() - started < 3


def test_guard_refuses_a_symbolic_link_loop(tmp_path: Path) -> None:
    """Refuse a path behind a link loop, which the system cannot open either."""
    policy = _policy(tmp_path)
    workspace = tmp_path / "work"
    (workspace / "loop1").symlink_to("loop2")
    (workspace / "loop2").symlink_to("loop1")
    with pytest.raises(ValueError, match="symbolic links"):
        harness_bridge.guard_check(policy, "Bash", {"command": "cat loop1"})


def test_guard_follows_a_short_link_chain_to_a_hidden_file(tmp_path: Path) -> None:
    """Resolve a hidden file behind a chain of links, as ``os.path.realpath`` does."""
    policy = _policy(tmp_path)
    workspace = tmp_path / "work"
    (workspace / "link2").symlink_to("optimize.toml")
    (workspace / "sub").mkdir()
    (workspace / "sub" / "link1").symlink_to("../link2")
    (workspace / "link0").symlink_to("sub/link1")
    assert harness_bridge.guard_check(policy, "Read", {"file_path": "link0"}) == "config"


def test_guard_still_follows_four_directory_changes(tmp_path: Path) -> None:
    """Resolve a hidden path behind the largest number of cd steps the guard follows."""
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
    """Refuse a call the guard cannot check well inside the hook timeout, and answer fast."""
    session = tmp_path / "session"
    harness_bridge.install_guard(session, tmp_path / "work", _HIDDEN)
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
    """Block a hidden read in the Pi extension, the OpenCode plugin and the eve module; let others run."""
    session = tmp_path / "session"
    env = harness_bridge.install_guard(session, tmp_path / "work", _HIDDEN)
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
try {{ await hooks["tool.execute.before"]({{ tool: "read" }}, {{ args: {{ filePath: "optimize.toml" }} }}); }}
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
    """Refuse the call when the policy is gone, changed or broken, or the input cannot be hashed."""
    session = tmp_path / "session"
    harness_bridge.install_guard(session, tmp_path / "work", _HIDDEN)
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
        hook_input = {"cmd": "cat src/\ud800.py optimize.toml"}
        check_input = {"file_path": "optimize.toml", "path": "src/\ud800.py"}
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
    """Block every call when the guard process fails, instead of letting it run."""
    session = tmp_path / "session"
    env = harness_bridge.install_guard(session, tmp_path / "work", _HIDDEN)
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
    """Install the guard for a session with hidden paths, report each refusal, and finish the session."""
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
        "hidden_paths": _HIDDEN,
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
    """Bind the guard to the HOME the agent runs with, so ``~`` resolves as in the agent's shell."""
    agent_home = tmp_path / "agent-home"
    session = tmp_path / "session"
    proposer = {**_PLAIN, "run_command": "echo done", "env": {"HOME": str(agent_home)}, "hidden_paths": _HIDDEN}
    run_session(proposer, workspace=tmp_path / "work", prompt="go", model="m1", session_dir=session)
    policy = json.loads((session / harness_bridge.GUARD_DIR / "policy.json").read_text())
    assert policy["home"] == str(agent_home)


def test_run_session_reports_host_agent_refusals(tmp_path: Path) -> None:
    """Take a host CLI turn's refusals from the shim's summary line, with no guard in the session."""
    line = json.dumps(
        {"type": "host_agent.result", "text": "ok", "usage": {}, "refusals": [{"tool": "Read", "kind": "config"}]}
    )
    proposer = {**_PLAIN, "output_format": "host_agent", "run_command": f"printf '%s\\n' '{line}'"}
    outcome = run_session(proposer, workspace=tmp_path / "work", prompt="go", model="m1", session_dir=tmp_path / "s")
    assert outcome.returncode == 0
    assert outcome.text == "ok"
    assert outcome.refusals == [{"tool": "Read", "kind": "config"}]


def test_run_session_without_hidden_paths_installs_no_guard(tmp_path: Path) -> None:
    """Leave sessions with no hidden list unchanged."""
    session = tmp_path / "session"
    outcome = run_session(_PLAIN, workspace=tmp_path / "work", prompt="go", model="m1", session_dir=session)
    assert outcome.refusals == []
    assert not (session / harness_bridge.GUARD_DIR).exists()


@pytest.mark.parametrize(
    "launch",
    [
        lambda gateway: harness._pi_launch("m1", gateway),
        lambda gateway: harness._prime_agent_launch("m1", gateway),
        lambda gateway: harness.pi_editor_launch("m1", gateway),
    ],
    ids=["pi", "prime", "pi-editor"],
)
def test_pi_family_launches_load_the_guard_extension_only_when_installed(launch, tmp_path: Path) -> None:
    """Add ``-e <guard>`` to Pi-family commands when the guard is set, and nothing when it is not."""
    command = launch(harness.GatewayConfig(url="https://gw.example/v1", api_key="k")).run_command
    assert harness._GUARD_PI_ARG in command
    expand = f"printf '%s|' {harness._GUARD_PI_ARG}"
    with_guard = subprocess.run(
        ["bash", "-c", expand], env={"APP_GUARD_PI": "/g/pi.ts"}, capture_output=True, text=True, check=True
    )
    without = subprocess.run(["bash", "-c", expand], env={}, capture_output=True, text=True, check=True)
    assert with_guard.stdout == "-e|/g/pi.ts|"
    assert without.stdout == "|"


def test_codex_launch_installs_the_guard_hooks_before_it_starts(tmp_path: Path) -> None:
    """Copy the guard hooks into the session's Codex home only when the guard is set."""
    command = harness._codex_launch("m1", harness.GatewayConfig(url="https://gw.example/v1", api_key="k")).run_command
    assert command.startswith(harness._GUARD_CODEX_COPY)
    (tmp_path / ".app" / "codex").mkdir(parents=True)
    (tmp_path / "hooks.json").write_text("{}")
    subprocess.run(["bash", "-c", harness._GUARD_CODEX_COPY], cwd=tmp_path, env={}, check=True)
    assert not (tmp_path / ".app" / "codex" / "hooks.json").exists()
    env = {"APP_GUARD_CODEX": str(tmp_path / "hooks.json")}
    subprocess.run(["bash", "-c", harness._GUARD_CODEX_COPY], cwd=tmp_path, env=env, check=True)
    assert (tmp_path / ".app" / "codex" / "hooks.json").read_text() == "{}"


# APP_TEST_CODEX can point at the pinned install (npm i @openai/codex@<CODEX_VERSION>).
_CODEX = os.environ.get("APP_TEST_CODEX") or shutil.which("codex")


class _FakeResponses(http.server.BaseHTTPRequestHandler):
    """Answer the Responses API: one ``cat optimize.toml`` call, then a final message."""

    tool_outputs: list[str] = []

    def log_message(self, *args: object) -> None:
        """Keep the test output quiet."""

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
            arguments = json.dumps({"cmd": "cat optimize.toml"})
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


@pytest.mark.skipif(_CODEX is None, reason="codex is not installed")
def test_installed_codex_runs_the_guard_hook_and_refuses_a_hidden_read(tmp_path: Path) -> None:
    """Drive the real Codex against a fake model: its shell read of optimize.toml is refused and logged."""
    assert _CODEX is not None
    _FakeResponses.tool_outputs = []
    server = http.server.ThreadingHTTPServer(("127.0.0.1", 0), _FakeResponses)
    threading.Thread(target=server.serve_forever, daemon=True).start()
    workspace = tmp_path / "work"
    workspace.mkdir()
    (workspace / "optimize.toml").write_text("secret = 1\n")
    gateway = harness.GatewayConfig(url=f"http://127.0.0.1:{server.server_address[1]}/v1", api_key="k")
    proposer = {
        **harness.launch_payload(harness._codex_launch("fake-model", gateway)),
        "hidden_paths": ["optimize.toml"],
    }
    path = f"{Path(_CODEX).parent}{os.pathsep}{os.environ.get('PATH', '')}"
    try:
        outcome = run_session(
            proposer,
            workspace=workspace,
            prompt="Read the config.",
            model="fake-model",
            session_dir=tmp_path / "session",
            timeout_seconds=120,
            extra_env={"PATH": path},
            api_key="k",
        )
    finally:
        server.shutdown()
    assert outcome.returncode == 0, outcome.stderr
    assert outcome.refusals == [{"tool": "Bash", "kind": "config"}]
    assert len(_FakeResponses.tool_outputs) == 1
    assert harness_bridge.GUARD_REFUSAL in _FakeResponses.tool_outputs[0]
    assert "secret" not in _FakeResponses.tool_outputs[0]


class _FakeChat(http.server.BaseHTTPRequestHandler):
    """Answer Chat Completions: one call of ``tool``, then a final message."""

    tool: tuple[str, dict[str, Any]] = ("read", {})
    tool_outputs: list[str] = []

    def log_message(self, *args: object) -> None:
        """Keep the test output quiet."""

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


# Each harness's own read tool and arguments. Prime Agent has only ``ipython``.
_CHAT_HARNESSES = [
    ("opencode", harness._opencode_launch, "read", "filePath"),
    ("pi", harness._pi_launch, "read", "path"),
    ("prime-agent", harness._prime_agent_launch, "ipython", "code"),
]


@pytest.mark.parametrize(("binary", "launch", "tool", "key"), _CHAT_HARNESSES, ids=[row[0] for row in _CHAT_HARNESSES])
@pytest.mark.parametrize("target", ["optimize.toml", "notes.txt"])
def test_installed_chat_harness_loads_the_guard_and_refuses_only_hidden_reads(
    tmp_path: Path, binary: str, launch: Any, tool: str, key: str, target: str
) -> None:
    """Drive the real OpenCode, Pi and Prime Agent against a fake model: only the hidden read is refused."""
    executable = shutil.which(binary)
    if executable is None:
        pytest.skip(f"{binary} is not installed")
    value = f'print(open("{target}").read())' if key == "code" else target
    _FakeChat.tool = (tool, {key: value})
    _FakeChat.tool_outputs = []
    server = http.server.ThreadingHTTPServer(("127.0.0.1", 0), _FakeChat)
    threading.Thread(target=server.serve_forever, daemon=True).start()
    workspace = tmp_path / "work"
    workspace.mkdir()
    (workspace / "optimize.toml").write_text("secret = 1\n")
    (workspace / "notes.txt").write_text("hello\n")
    (tmp_path / "home").mkdir()
    gateway = harness.GatewayConfig(url=f"http://127.0.0.1:{server.server_address[1]}/v1", api_key="k")
    proposer = {**harness.launch_payload(launch("fake-model", gateway)), "hidden_paths": ["optimize.toml"]}
    path = f"{Path(executable).parent}{os.pathsep}{os.environ.get('PATH', '')}"
    try:
        outcome = run_session(
            proposer,
            workspace=workspace,
            prompt="Read the file.",
            model="fake-model",
            session_dir=tmp_path / "session",
            timeout_seconds=120,
            extra_env={"PATH": path, "HOME": str(tmp_path / "home")},
            api_key="k",
        )
    finally:
        server.shutdown()
    assert outcome.returncode == 0, outcome.stderr
    assert len(_FakeChat.tool_outputs) == 1
    if target == "optimize.toml":
        assert outcome.refusals == [{"tool": tool, "kind": "config"}]
        assert _FakeChat.tool_outputs[0] == harness_bridge.GUARD_REFUSAL
    else:
        assert outcome.refusals == []
        assert "hello" in _FakeChat.tool_outputs[0]
