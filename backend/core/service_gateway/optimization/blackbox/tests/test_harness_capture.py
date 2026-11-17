"""Tests for harness capture: tool call pairs and exec shim records as step commits, and the session tree."""

from __future__ import annotations

import contextlib
import json
import os
import stat
import subprocess
import sys
import threading
from collections.abc import Iterator
from pathlib import Path
from typing import Any

import pytest
from sqlalchemy import create_engine
from sqlalchemy.engine import Engine
from sqlalchemy.orm import Session

from core.storage.models import Base, JobModel
from core.storage.object_store import LocalObjectStore
from core.storage.step_log_store import StepLogStore
from core.storage.tree_store import TreeStore

from .. import exec_shim, harness_bridge, harness_capture
from ..harness_bridge import SESSIONS_DIR, install_shim
from ..harness_capture import HarnessCapture, Snapshot, restore_session, snapshot_session

RUN = "job-ada"
KEY = "fake-route-key"
_NO_LAUNCH: dict[str, Any] = {}


def _snapshot(trees: TreeStore, workspace: Path, session_dir: Path, proposer: dict[str, Any] | None = None) -> Snapshot:
    """Take a session snapshot with the test route key.

    Args:
        trees: The tree store.
        workspace: The agent's workspace.
        session_dir: The bridge's session folder.
        proposer: The launch; none by default.

    Returns:
        The snapshot: the root tree id and the skipped files.
    """
    return snapshot_session(trees, RUN, workspace, session_dir, proposer=proposer or _NO_LAUNCH, route_key=KEY)


@pytest.fixture
def engine(tmp_path: Path) -> Engine:
    """Return a SQLite file engine with the schema and one job.

    Args:
        tmp_path: Pytest's temporary folder.

    Returns:
        The engine.
    """
    engine = create_engine(f"sqlite:///{tmp_path / 'db.sqlite'}")
    Base.metadata.create_all(engine)
    with Session(engine) as session:
        session.add(JobModel(optimization_id=RUN, username="ada", execution_generation=1))
        session.commit()
    return engine


@pytest.fixture
def step_log(engine: Engine, tmp_path: Path) -> Iterator[StepLogStore]:
    """Yield a step log with a long window, so only explicit flushes write.

    Args:
        engine: The test engine.
        tmp_path: Pytest's temporary folder.

    Yields:
        The step log; it closes after the test.
    """
    store = StepLogStore(engine, LocalObjectStore(tmp_path / "objects"), "w1", flush_window_seconds=60.0)
    yield store
    store.close()


def _capture(step_log: StepLogStore, workspace: Path, session_dir: Path, **kwargs: Any) -> HarnessCapture:
    """Build a capture whose snapshot reads the real workspace and session folder.

    Args:
        step_log: The test step log.
        workspace: The agent's workspace.
        session_dir: The bridge's session folder.
        **kwargs: Extra capture arguments, and ``proposer`` for the snapshot.

    Returns:
        The capture.
    """
    trees = TreeStore(step_log)
    proposer = kwargs.pop("proposer", None)
    return HarnessCapture(
        step_log,
        run_id=RUN,
        agent_id="proposer",
        execution_generation=1,
        grant_list_id="default",
        snapshot=lambda: _snapshot(trees, workspace, session_dir, proposer),
        route_key=KEY,
        **kwargs,
    )


def _tool_round(call_id: str, command: str, output: str) -> dict[str, Any]:
    """Build an Anthropic Messages request whose last turn answers one bash tool call.

    Args:
        call_id: The tool_use id.
        command: The bash command the model asked for.
        output: The tool output the harness sends back.

    Returns:
        The request body.
    """
    return {
        "model": "m1",
        "messages": [
            {"role": "user", "content": "fix the bug"},
            {
                "role": "assistant",
                "content": [
                    {"type": "text", "text": "Running it."},
                    {"type": "tool_use", "id": call_id, "name": "bash", "input": {"command": command}},
                ],
            },
            {"role": "user", "content": [{"type": "tool_result", "tool_use_id": call_id, "content": output}]},
        ],
    }


def _tree_files(trees: TreeStore, tree_id: str, prefix: str = "") -> dict[str, bytes]:
    """Flatten a stored tree into file paths and bytes.

    Args:
        trees: The tree store.
        tree_id: The root tree id.
        prefix: Path prefix of this subtree.

    Returns:
        Every file path below the tree and its bytes.
    """
    files: dict[str, bytes] = {}
    for entry in trees.get_tree(RUN, tree_id) or []:
        path = prefix + entry.name
        if entry.mode == "tree":
            files.update(_tree_files(trees, entry.object_id, path + "/"))
        else:
            files[path] = trees.get_blob(RUN, entry.object_id) or b""
    return files


def test_each_tool_use_and_tool_result_pair_is_one_step_with_command_output_and_tree(step_log, tmp_path) -> None:
    """A paired call commits one command step that names the command, the output and the tree after it."""
    workspace, session_dir = tmp_path / "work", tmp_path / "home" / SESSIONS_DIR / "s1"
    workspace.mkdir()
    session_dir.mkdir(parents=True)
    (workspace / "app.py").write_text("print(2)\n")
    (session_dir / "opening-prompt.md").write_text("fix the bug")
    capture = _capture(step_log, workspace, session_dir)

    [step_id] = capture.observe(_tool_round("t1", "pytest -q", "1 passed"))

    step = step_log.get_step(step_id)
    assert step_id == f"{RUN}:proposer:0"
    assert step["kind"] == "command"
    assert step["body"] == {
        "source": "tool_use",
        "tool_use_id": "t1",
        "tool": "bash",
        "command": {"command": "pytest -q"},
        "output": "1 passed",
        "is_error": False,
    }
    files = _tree_files(TreeStore(step_log), step["tree_id"])
    assert files["app.py"] == b"print(2)\n"
    assert files[f"{SESSIONS_DIR}/s1/opening-prompt.md"] == b"fix the bug"


def test_a_repeated_request_or_an_unanswered_call_commits_nothing_more(step_log, tmp_path) -> None:
    """A retried request does not commit its pair twice, and a call with no result yet waits for it."""
    workspace, session_dir = tmp_path / "work", tmp_path / "session"
    workspace.mkdir()
    session_dir.mkdir()
    snapshots: list[str] = []
    trees = TreeStore(step_log)

    def snapshot() -> Snapshot:
        """Count each snapshot and take it.

        Returns:
            The snapshot.
        """
        snapshots.append("taken")
        return _snapshot(trees, workspace, session_dir)

    capture = HarnessCapture(
        step_log,
        run_id=RUN,
        agent_id="proposer",
        execution_generation=1,
        grant_list_id="default",
        snapshot=snapshot,
        route_key=KEY,
        seq=5,
    )
    request = _tool_round("t1", "ls", "app.py")
    unanswered = {**request, "messages": request["messages"][:2]}

    assert capture.observe(unanswered) == []
    assert capture.observe(request) == [f"{RUN}:proposer:5"]
    assert capture.observe(request) == []
    assert len(snapshots) == 1
    two = _tool_round("t2", "cat a", "x")
    two["messages"][1]["content"].append({"type": "tool_use", "id": "t3", "name": "read", "input": {"path": "b"}})
    two["messages"][2]["content"].append(
        {
            "type": "tool_result",
            "tool_use_id": "t3",
            "content": [{"type": "text", "text": "no such file"}],
            "is_error": True,
        }
    )
    first, second = capture.observe(two)
    assert len(snapshots) == 2
    assert step_log.get_step(first)["tree_id"] == step_log.get_step(second)["tree_id"]
    assert step_log.get_step(second)["body"]["output"] == "no such file"
    assert step_log.get_step(second)["body"]["is_error"] is True


def test_exec_shim_records_become_steps_before_the_next_model_call(step_log, tmp_path) -> None:
    """Commands the shim logged with no model call commit in order, and a line the agent broke is skipped."""
    workspace, session_dir = tmp_path / "work", tmp_path / "session"
    workspace.mkdir()
    session_dir.mkdir()
    log = session_dir / harness_bridge.EXEC_LOG
    capture = _capture(step_log, workspace, session_dir, exec_log=log)
    log.write_text(
        json.dumps({"command": "make test", "exit_code": 2, "stdout": "FAIL\n", "stderr": "boom\n"})
        + "\nnot json\n[1]\n"
        + json.dumps({"command": "ls", "exit_code": 0, "stdout": "a\n", "stderr": ""})
        + "\n"
    )

    steps = capture.observe({"model": "m1", "messages": [{"role": "user", "content": "go"}]})
    assert [step_log.get_step(step)["body"] for step in steps] == [
        {"source": "exec_shim", "command": "make test", "exit_code": 2, "stdout": "FAIL\n", "stderr": "boom\n"},
        {"source": "exec_shim", "command": "ls", "exit_code": 0, "stdout": "a\n", "stderr": ""},
    ]
    with log.open("a") as handle:
        handle.write(json.dumps({"command": "pwd", "exit_code": 0, "stdout": "/w\n", "stderr": ""}) + "\n")
        handle.write('{"command": "half", "exit_code": 0, ')
    [last] = capture.finish()
    assert step_log.get_step(last)["body"]["command"] == "pwd"
    assert last == f"{RUN}:proposer:2"
    with log.open("a") as handle:
        handle.write('"stdout": "", "stderr": ""}\n')
    [late] = capture.finish()
    assert step_log.get_step(late)["body"]["command"] == "half"
    assert capture.finish() == []


def test_no_stored_object_holds_the_route_key(step_log, tmp_path) -> None:
    """Launch files, logs, copies, tool output and exec records never put the key into the step log or tree store."""
    workspace, session_dir, bin_dir = tmp_path / "work", tmp_path / "session", tmp_path / "bin"
    bin_dir.mkdir()
    (bin_dir / "probe").write_text('#!/bin/sh\necho "probe $APP_API_KEY"\n')
    (bin_dir / "probe").chmod(0o755)
    proposer = {
        "run_command": 'env > env.txt; cp .codex/config.toml copy.toml; touch "name-$APP_API_KEY"; probe; echo "$APP_API_KEY"',
        "files": {".codex/config.toml": f'key = "{harness_bridge.KEY_TOKEN}"'},
        "instructions_file": "AGENTS.md",
        "env": {"APP_API_KEY": harness_bridge.KEY_TOKEN, "PATH": f"{bin_dir}{os.pathsep}{os.environ['PATH']}"},
        "exec_commands": ["probe"],
        "output_format": "plain",
    }
    capture = _capture(
        step_log, workspace, session_dir, proposer=proposer, exec_log=session_dir / harness_bridge.EXEC_LOG
    )
    harness_bridge.run_session(
        proposer, workspace=workspace, prompt="go", model="m1", session_dir=session_dir, api_key=KEY
    )
    assert KEY in (workspace / "copy.toml").read_text()

    [shim_step] = capture.observe({"model": "m1", "messages": [{"role": "user", "content": "go"}]})
    [tool_step] = capture.observe(_tool_round("t1", f"curl -H 'x-api-key: {KEY}'", f"APP_API_KEY={KEY}"))

    assert step_log.get_step(shim_step)["body"]["stdout"] == f"probe {harness_bridge.KEY_TOKEN}\n"
    assert step_log.get_step(tool_step)["body"]["output"] == f"APP_API_KEY={harness_bridge.KEY_TOKEN}"
    files = _tree_files(TreeStore(step_log), step_log.get_step(tool_step)["tree_id"])
    assert files["copy.toml"] == f'key = "{harness_bridge.KEY_TOKEN}"'.encode()
    assert ".codex/config.toml" not in files
    assert "AGENTS.md" not in files
    assert not [path for path in files if "/run-" in path or "exec-" in path]
    step_log.flush()
    stored = [path for path in (tmp_path / "objects").rglob("*") if path.is_file()]
    stored += list(tmp_path.glob("db.sqlite*"))
    assert len(stored) > 1
    assert [path for path in stored if KEY.encode() in path.read_bytes()] == []


def test_a_listed_command_inside_a_model_tool_call_commits_once(step_log, tmp_path) -> None:
    """When a request answers tool calls, the exec records of that interval belong to them and do not commit."""
    workspace, session_dir = tmp_path / "work", tmp_path / "session"
    workspace.mkdir()
    session_dir.mkdir()
    log = session_dir / harness_bridge.EXEC_LOG
    capture = _capture(step_log, workspace, session_dir, exec_log=log)
    log.write_text(json.dumps({"command": "make", "exit_code": 0, "stdout": "ok\n", "stderr": ""}) + "\n")

    [step] = capture.observe(_tool_round("t1", "make", "ok"))

    assert step_log.get_step(step)["body"]["source"] == "tool_use"
    assert capture.finish() == []


def test_a_command_the_harness_ran_on_its_own_next_to_a_tool_call_is_dropped(step_log, tmp_path) -> None:
    """The known gap, on purpose: a request with a new tool result drops every exec record of its interval."""
    workspace, session_dir = tmp_path / "work", tmp_path / "session"
    workspace.mkdir()
    session_dir.mkdir()
    log = session_dir / harness_bridge.EXEC_LOG
    capture = _capture(step_log, workspace, session_dir, exec_log=log)
    log.write_text(
        json.dumps({"command": "make", "exit_code": 0, "stdout": "ok\n", "stderr": ""})
        + "\n"
        + json.dumps({"command": "git status", "exit_code": 0, "stdout": "clean\n", "stderr": ""})
        + "\n"
    )

    steps = capture.observe(_tool_round("t1", "make", "ok"))

    assert [step_log.get_step(step)["body"]["source"] for step in steps] == ["tool_use"]
    assert capture.finish() == []


def test_a_failed_snapshot_keeps_the_exec_records_for_the_next_commit(step_log, tmp_path) -> None:
    """The log position moves only after the commit, so a snapshot error loses no command."""
    workspace, session_dir = tmp_path / "work", tmp_path / "session"
    workspace.mkdir()
    session_dir.mkdir()
    log = session_dir / harness_bridge.EXEC_LOG
    trees = TreeStore(step_log)
    failures = [OSError("disk full")]

    def snapshot() -> Snapshot:
        """Fail once, then take the snapshot.

        Returns:
            The snapshot.

        Raises:
            OSError: On the first call.
        """
        if failures:
            raise failures.pop()
        return _snapshot(trees, workspace, session_dir)

    capture = HarnessCapture(
        step_log,
        run_id=RUN,
        agent_id="proposer",
        execution_generation=1,
        grant_list_id="default",
        snapshot=snapshot,
        route_key=KEY,
        exec_log=log,
    )
    log.write_text(json.dumps({"command": "make", "exit_code": 0, "stdout": "", "stderr": ""}) + "\n")

    with pytest.raises(OSError, match="disk full"):
        capture.finish()
    [step] = capture.finish()
    assert step_log.get_step(step)["body"]["command"] == "make"


def test_a_restored_session_does_not_commit_old_exec_records_again(step_log, tmp_path) -> None:
    """The exec log stays out of the tree, so a capture in the new box starts with nothing to commit."""
    workspace, session_dir = tmp_path / "work", tmp_path / "home" / SESSIONS_DIR / "s1"
    workspace.mkdir()
    session_dir.mkdir(parents=True)
    log = session_dir / harness_bridge.EXEC_LOG
    first_capture = _capture(step_log, workspace, session_dir, exec_log=log)
    log.write_text(json.dumps({"command": "make", "exit_code": 0, "stdout": "", "stderr": ""}) + "\n")
    [first] = first_capture.finish()
    tree_id = step_log.get_step(first)["tree_id"]

    new_home, new_workspace = tmp_path / "home-2", tmp_path / "work-2"
    restore_session(TreeStore(step_log), RUN, tree_id, new_workspace, new_home)
    new_session = new_home / SESSIONS_DIR / "s1"
    capture = _capture(step_log, new_workspace, new_session, exec_log=new_session / harness_bridge.EXEC_LOG, seq=1)

    assert capture.finish() == []


def _record(command: str) -> str:
    """Build one exec log line.

    Args:
        command: The command line.

    Returns:
        The JSON line, with its newline.
    """
    return json.dumps({"command": command, "exit_code": 0, "stdout": "", "stderr": ""}) + "\n"


def test_a_capture_for_a_follow_up_turn_does_not_commit_the_earlier_turns_records(step_log, tmp_path) -> None:
    """The exec log is per session; a new capture on the same session folder reads only the lines after its start."""
    workspace, session_dir = tmp_path / "work", tmp_path / "session"
    workspace.mkdir()
    session_dir.mkdir()
    log = session_dir / harness_bridge.EXEC_LOG
    first = _capture(step_log, workspace, session_dir, exec_log=log)
    log.write_text(_record("make"))
    assert len(first.finish()) == 1

    second = _capture(step_log, workspace, session_dir, exec_log=log, seq=1)
    with log.open("a") as handle:
        handle.write(_record("ls"))

    [step] = second.finish()
    assert step_log.get_step(step)["body"]["command"] == "ls"


@pytest.mark.parametrize("to_device", [False, True])
def test_an_exec_log_replaced_by_a_link_is_not_read(step_log, tmp_path, to_device) -> None:
    """The agent can point the log at a host file or at ``/dev/zero``; the capture reads neither."""
    workspace, session_dir, target = tmp_path / "work", tmp_path / "session", tmp_path / "host-log"
    workspace.mkdir()
    session_dir.mkdir()
    target.write_text("")
    log = session_dir / harness_bridge.EXEC_LOG
    log.symlink_to("/dev/zero" if to_device else target)
    capture = _capture(step_log, workspace, session_dir, exec_log=log)
    target.write_text(_record("cat host-secret"))

    assert capture.finish() == []


def test_an_exec_log_replaced_by_a_folder_is_not_read(step_log, tmp_path) -> None:
    """A log path that is not a regular file and not a link reads as empty and does not break the capture."""
    workspace, session_dir = tmp_path / "work", tmp_path / "session"
    workspace.mkdir()
    session_dir.mkdir()
    log = session_dir / harness_bridge.EXEC_LOG
    log.mkdir()
    capture = _capture(step_log, workspace, session_dir, exec_log=log)

    assert capture.observe({"model": "m1", "messages": [{"role": "user", "content": "go"}]}) == []
    assert capture.finish() == []


def test_an_exec_log_replaced_by_a_pipe_does_not_hang_the_capture(step_log, tmp_path) -> None:
    """A FIFO log with no writer is opened without blocking and read as empty."""
    workspace, session_dir = tmp_path / "work", tmp_path / "session"
    workspace.mkdir()
    session_dir.mkdir()
    log = session_dir / harness_bridge.EXEC_LOG
    os.mkfifo(log)
    result: list[list[str]] = []
    worker = threading.Thread(
        target=lambda: result.append(_capture(step_log, workspace, session_dir, exec_log=log).finish()), daemon=True
    )
    worker.start()
    worker.join(timeout=10)
    try:
        assert not worker.is_alive()
    finally:
        if worker.is_alive():
            # A writer ends the blocked open, so the thread does not outlive the test.
            with contextlib.suppress(OSError):
                os.close(os.open(log, os.O_WRONLY | os.O_NONBLOCK))
    assert result == [[]]


def test_one_finish_reads_a_log_larger_than_the_read_limit(step_log, tmp_path, monkeypatch) -> None:
    """One finish reads the log in limit-sized chunks to its end, and a line longer than the limit is skipped whole."""
    workspace, session_dir = tmp_path / "work", tmp_path / "session"
    workspace.mkdir()
    session_dir.mkdir()
    log = session_dir / harness_bridge.EXEC_LOG
    make, ls = _record("make"), _record("ls")
    limit = len(make) + 5
    monkeypatch.setattr(harness_capture, "_EXEC_READ_LIMIT", limit)
    capture = _capture(step_log, workspace, session_dir, exec_log=log)
    log.write_text("x" * limit + "\n" + make + ls)

    steps = capture.finish()

    assert [step_log.get_step(step)["body"]["command"] for step in steps] == ["make", "ls"]
    assert capture.finish() == []


def test_a_drain_ends_while_a_writer_keeps_appending_to_the_exec_log(step_log, tmp_path, monkeypatch) -> None:
    """One finish reads up to the log size at its start; lines a child adds during it wait for the next call."""
    workspace, session_dir = tmp_path / "work", tmp_path / "session"
    workspace.mkdir()
    session_dir.mkdir()
    log = session_dir / harness_bridge.EXEC_LOG
    make = _record("make")
    monkeypatch.setattr(harness_capture, "_EXEC_READ_LIMIT", len(make) + 5)
    capture = _capture(step_log, workspace, session_dir, exec_log=log)
    log.write_text(make * 2)
    real_read, reads = capture._exec_records, []

    def read_while_a_writer_appends() -> tuple[list[tuple[dict[str, Any], int]], int]:
        """Append one record before each read, as a child that never stops; fail instead of looping without end.

        Returns:
            The real read's records and position.
        """
        reads.append(1)
        assert len(reads) < 50, "the drain does not end"
        with log.open("a") as handle:
            handle.write(make)
        return real_read()

    monkeypatch.setattr(capture, "_exec_records", read_while_a_writer_appends)

    assert len(capture.finish()) == 2
    assert len(reads) == 2


def test_a_tool_round_skips_every_exec_record_of_a_log_larger_than_the_read_limit(
    step_log, tmp_path, monkeypatch
) -> None:
    """A tool round moves the log position to the end, so the next model call commits none of those records."""
    workspace, session_dir = tmp_path / "work", tmp_path / "session"
    workspace.mkdir()
    session_dir.mkdir()
    log = session_dir / harness_bridge.EXEC_LOG
    make = _record("make")
    monkeypatch.setattr(harness_capture, "_EXEC_READ_LIMIT", len(make) + 5)
    capture = _capture(step_log, workspace, session_dir, exec_log=log)
    log.write_text(make * 3)

    assert len(capture.observe(_tool_round("t1", "make", "ok"))) == 1
    assert capture.finish() == []


def test_a_retry_after_a_failed_commit_does_not_commit_the_earlier_pairs_again(step_log, tmp_path) -> None:
    """Each pair is recorded right after its commit, so a retry commits only the pairs that did not commit."""
    workspace, session_dir = tmp_path / "work", tmp_path / "session"
    workspace.mkdir()
    session_dir.mkdir()
    capture = _capture(step_log, workspace, session_dir)
    request = _tool_round("t1", "make", "ok")
    request["messages"][1]["content"].append(
        {"type": "tool_use", "id": "t2", "name": "bash", "input": {"command": "ls"}}
    )
    request["messages"][2]["content"].append({"type": "tool_result", "tool_use_id": "t2", "content": "a"})
    real_commit, calls = step_log.commit, []

    def flaky_commit(**kwargs: Any) -> str:
        """Fail on the second call, else commit.

        Args:
            **kwargs: The commit arguments.

        Returns:
            The step id.

        Raises:
            RuntimeError: On the second call.
        """
        calls.append(kwargs["seq"])
        if len(calls) == 2:
            raise RuntimeError("fence")
        return real_commit(**kwargs)

    step_log.commit = flaky_commit
    with pytest.raises(RuntimeError, match="fence"):
        capture.observe(request)
    [retry] = capture.observe(request)

    assert calls == [0, 1, 1]
    assert step_log.get_step(retry)["body"]["command"] == {"command": "ls"}


def test_a_retry_after_a_failed_commit_does_not_commit_the_earlier_exec_records_again(step_log, tmp_path) -> None:
    """Each exec record moves the log position right after its commit, so a retry starts at the failed one."""
    workspace, session_dir = tmp_path / "work", tmp_path / "session"
    workspace.mkdir()
    session_dir.mkdir()
    log = session_dir / harness_bridge.EXEC_LOG
    capture = _capture(step_log, workspace, session_dir, exec_log=log)
    log.write_text(_record("make") + _record("ls"))
    real_commit, calls = step_log.commit, []

    def flaky_commit(**kwargs: Any) -> str:
        """Fail on the second call, else commit.

        Args:
            **kwargs: The commit arguments.

        Returns:
            The step id.

        Raises:
            RuntimeError: On the second call.
        """
        calls.append(kwargs["body"]["command"])
        if len(calls) == 2:
            raise RuntimeError("fence")
        return real_commit(**kwargs)

    step_log.commit = flaky_commit
    with pytest.raises(RuntimeError, match="fence"):
        capture.finish()
    capture.finish()

    assert calls == ["make", "ls", "ls"]


@pytest.mark.parametrize(
    "use",
    [
        {"type": "tool_use", "id": ["t1"], "name": "bash", "input": {}},
        {"type": "tool_use", "id": {"a": 1}, "name": "bash", "input": {}},
        {"type": "tool_use", "name": "bash", "input": {}},
        {"type": "tool_use", "id": "", "name": "bash", "input": {}},
    ],
)
def test_a_tool_call_with_a_bad_id_is_skipped(step_log, tmp_path, use) -> None:
    """A list, dict, empty or missing id does not break the capture; only the well-formed pair commits."""
    workspace, session_dir = tmp_path / "work", tmp_path / "session"
    workspace.mkdir()
    session_dir.mkdir()
    capture = _capture(step_log, workspace, session_dir)
    request = _tool_round("t2", "ls", "a")
    request["messages"][1]["content"].append(use)
    request["messages"][2]["content"].append({"type": "tool_result", "tool_use_id": use.get("id"), "content": "x"})

    [step] = capture.observe(request)

    assert step_log.get_step(step)["body"]["command"] == {"command": "ls"}


def test_a_tool_call_with_an_input_too_deep_to_redact_commits_with_no_input(step_log, tmp_path) -> None:
    """The call still ran, so it commits a step with its id and tool and no input, and the capture goes on."""
    workspace, session_dir = tmp_path / "work", tmp_path / "session"
    workspace.mkdir()
    session_dir.mkdir()
    capture = _capture(step_log, workspace, session_dir)
    deep: dict[str, Any] = {}
    for _ in range(5000):
        deep = {"a": deep}
    request = _tool_round("t2", "ls", "a")
    request["messages"][1]["content"].append({"type": "tool_use", "id": "t1", "name": "bash", "input": deep})
    request["messages"][2]["content"].append({"type": "tool_result", "tool_use_id": "t1", "content": "x"})

    steps = capture.observe(request)

    assert [step_log.get_step(step)["body"] for step in steps] == [
        {
            "source": "tool_use",
            "tool_use_id": "t2",
            "tool": "bash",
            "command": {"command": "ls"},
            "output": "a",
            "is_error": False,
        },
        {"source": "tool_use", "tool_use_id": "t1", "tool": "bash", "input_too_deep": True},
    ]
    assert capture.observe(request) == []


def test_a_route_key_cut_at_the_start_of_an_output_tail_is_not_stored(step_log, tmp_path) -> None:
    """The shim keeps the last bytes of a stream; a key part at the start of a full tail is dropped."""
    workspace, session_dir = tmp_path / "work", tmp_path / "session"
    workspace.mkdir()
    session_dir.mkdir()
    log = session_dir / harness_bridge.EXEC_LOG
    capture = _capture(step_log, workspace, session_dir, exec_log=log)
    tail = KEY[3:] + "y" * (exec_shim.OUTPUT_LIMIT - len(KEY) + 3)
    log.write_text(json.dumps({"command": "make", "exit_code": 0, "stdout": tail, "stderr": "short"}) + "\n")

    [step] = capture.finish()

    body = step_log.get_step(step)["body"]
    assert KEY[3:] not in body["stdout"]
    assert body["stdout"].endswith("y" * 100)
    assert body["stderr"] == "short"


def test_a_deep_folder_tree_is_cut_at_the_depth_limit(step_log, tmp_path) -> None:
    """A tree deeper than the recursion limit does not break the snapshot; folders below the limit are left out."""
    workspace, session_dir = tmp_path / "work", tmp_path / "session"
    workspace.mkdir()
    session_dir.mkdir()
    # Build by descriptor: the full path is longer than PATH_MAX.
    fd = os.open(workspace, os.O_RDONLY | os.O_DIRECTORY)
    try:
        for _ in range(600):
            os.close(os.open("f", os.O_WRONLY | os.O_CREAT, 0o644, dir_fd=fd))
            os.mkdir("d", dir_fd=fd)
            child = os.open("d", os.O_RDONLY | os.O_DIRECTORY, dir_fd=fd)
            os.close(fd)
            fd = child
    finally:
        os.close(fd)
    trees = TreeStore(step_log)

    files = _tree_files(trees, _snapshot(trees, workspace, session_dir).tree_id)

    assert max(path.count("/") for path in files) == harness_capture._MAX_DEPTH
    assert "f" in files


def test_a_file_over_the_size_cap_is_left_out_and_named_also_when_sparse(step_log, tmp_path, monkeypatch) -> None:
    """The size check comes before the read: a huge sparse file costs no memory, and the result names each one."""
    monkeypatch.setattr(harness_capture, "_FILE_SIZE_LIMIT", 10)
    workspace, session_dir = tmp_path / "work", tmp_path / "home" / SESSIONS_DIR / "s1"
    workspace.mkdir()
    session_dir.mkdir(parents=True)
    (workspace / "small.txt").write_bytes(b"x" * 10)
    (workspace / "big.txt").write_bytes(b"x" * 11)
    (session_dir / "notes.md").write_bytes(b"y" * 11)
    with (workspace / "sparse.bin").open("wb") as handle:
        handle.truncate(1 << 40)
    reads: list[int] = []
    real_read = os.read

    def counted_read(fd: int, size: int) -> bytes:
        """Read and record the requested size.

        Args:
            fd: The descriptor.
            size: The most bytes to read.

        Returns:
            The bytes read.
        """
        reads.append(size)
        return real_read(fd, size)

    monkeypatch.setattr(harness_capture.os, "read", counted_read)
    trees = TreeStore(step_log)

    snapshot = _snapshot(trees, workspace, session_dir)

    assert set(_tree_files(trees, snapshot.tree_id)) == {"small.txt"}
    assert snapshot.skipped == ["big.txt", "sparse.bin", f"{SESSIONS_DIR}/s1/notes.md"]
    assert snapshot.skipped_count == 3
    # Only small.txt is read: one chunk of the cap plus one byte, then the end of the file.
    assert reads == [11, 1]


def test_a_file_that_grows_past_the_size_cap_during_the_read_is_left_out(step_log, tmp_path, monkeypatch) -> None:
    """The read stops one byte past the cap, so a file that keeps growing is cut off and named."""
    monkeypatch.setattr(harness_capture, "_FILE_SIZE_LIMIT", 10)
    workspace, session_dir = tmp_path / "work", tmp_path / "session"
    workspace.mkdir()
    session_dir.mkdir()
    (workspace / "grows.log").write_bytes(b"x" * 10)
    reads: list[int] = []

    def endless_read(_fd: int, size: int) -> bytes:
        """Return a full chunk each time, like a file a writer keeps appending to.

        Args:
            _fd: The descriptor.
            size: The most bytes to read.

        Returns:
            ``size`` bytes.
        """
        reads.append(size)
        return b"g" * size

    monkeypatch.setattr(harness_capture.os, "read", endless_read)
    trees = TreeStore(step_log)

    snapshot = _snapshot(trees, workspace, session_dir)

    assert _tree_files(trees, snapshot.tree_id) == {}
    assert (snapshot.skipped, snapshot.skipped_count) == (["grows.log"], 1)
    assert sum(reads) == 11


def test_files_past_the_total_size_cap_are_left_out_and_smaller_ones_still_fit(step_log, tmp_path, monkeypatch) -> None:
    """The snapshot stops at the total cap: a file that does not fit in what is left is named, a smaller one is kept."""
    monkeypatch.setattr(harness_capture, "_TOTAL_SIZE_LIMIT", 10)
    workspace, session_dir = tmp_path / "work", tmp_path / "session"
    workspace.mkdir()
    session_dir.mkdir()
    (workspace / "a.txt").write_bytes(b"a" * 6)
    (workspace / "b.txt").write_bytes(b"b" * 6)
    (session_dir / "c.txt").write_bytes(b"c" * 4)
    trees = TreeStore(step_log)

    snapshot = _snapshot(trees, workspace, session_dir)

    assert set(_tree_files(trees, snapshot.tree_id)) == {"a.txt", f"{SESSIONS_DIR}/session/c.txt"}
    assert (snapshot.skipped, snapshot.skipped_count) == (["b.txt"], 1)


def test_files_and_links_past_the_count_cap_are_left_out_and_all_counted(step_log, tmp_path, monkeypatch) -> None:
    """Past the file count cap each file and link is left out; the result lists the first few and counts them all."""
    monkeypatch.setattr(harness_capture, "_FILE_COUNT_LIMIT", 2)
    monkeypatch.setattr(harness_capture, "_SKIPPED_LISTED", 2)
    workspace, session_dir = tmp_path / "work", tmp_path / "session"
    (workspace / "src").mkdir(parents=True)
    session_dir.mkdir()
    for name in ("a.txt", "b.txt", "src/c.txt"):
        (workspace / name).write_text(name)
    (workspace / "d.link").symlink_to("a.txt")
    (session_dir / "e.txt").write_text("e")
    trees = TreeStore(step_log)

    snapshot = _snapshot(trees, workspace, session_dir)

    assert set(_tree_files(trees, snapshot.tree_id)) == {"a.txt", "b.txt"}
    assert snapshot.skipped == ["d.link", "src/c.txt"]
    assert snapshot.skipped_count == 3


def test_each_step_names_the_files_the_snapshot_caps_left_out(step_log, tmp_path, monkeypatch) -> None:
    """A step whose tree misses a capped file says so in its body, so a reader knows the tree is not whole."""
    monkeypatch.setattr(harness_capture, "_FILE_SIZE_LIMIT", 10)
    workspace, session_dir = tmp_path / "work", tmp_path / "session"
    workspace.mkdir()
    session_dir.mkdir()
    (workspace / "big.bin").write_bytes(b"x" * 11)
    capture = _capture(step_log, workspace, session_dir)

    [step_id] = capture.observe(_tool_round("t1", "ls", "big.bin"))

    body = step_log.get_step(step_id)["body"]
    assert (body["snapshot_skipped"], body["snapshot_skipped_count"]) == (["big.bin"], 1)
    assert body["command"] == {"command": "ls"}


def test_unreadable_files_are_left_out_and_the_snapshot_still_succeeds(step_log, tmp_path) -> None:
    """A file or folder the agent made unreadable does not stop the snapshot."""
    workspace, session_dir = tmp_path / "work", tmp_path / "session"
    (workspace / "locked-dir").mkdir(parents=True)
    session_dir.mkdir()
    (workspace / "main.py").write_text("x = 1\n")
    (workspace / "secret.txt").write_text("x")
    (workspace / "secret.txt").chmod(0)
    (workspace / "locked-dir").chmod(0)
    try:
        tree_id = _snapshot(TreeStore(step_log), workspace, session_dir).tree_id
    finally:
        (workspace / "secret.txt").chmod(0o644)
        (workspace / "locked-dir").chmod(0o755)

    assert set(_tree_files(TreeStore(step_log), tree_id)) == {"main.py"}


def _lstat_lies(monkeypatch: pytest.MonkeyPatch, name: str, result: os.stat_result) -> None:
    """Make the walk's ``lstat`` of one entry return ``result``, as if the entry changed right after it.

    Args:
        monkeypatch: Pytest's monkeypatch fixture.
        name: The entry name.
        result: The stat result the walk sees.
    """
    real_stat = os.stat

    def fake_stat(path: Any, *args: Any, dir_fd: int | None = None, follow_symlinks: bool = True) -> os.stat_result:
        """Return ``result`` for the entry, else the real stat.

        Args:
            path: The path.
            *args: Other positional arguments.
            dir_fd: The folder descriptor.
            follow_symlinks: Whether to follow a link.

        Returns:
            The stat result.
        """
        if dir_fd is not None and path == name:
            return result
        return real_stat(path, *args, dir_fd=dir_fd, follow_symlinks=follow_symlinks)

    monkeypatch.setattr(os, "stat", fake_stat)


def _as_regular_file(info: os.stat_result) -> os.stat_result:
    """Return ``info`` with the mode of a regular file.

    Args:
        info: A real stat result.

    Returns:
        The same inode and device, typed as a regular file.
    """
    fields = list(info)[:10]
    fields[0] = stat.S_IFREG | 0o644
    return os.stat_result(fields)


def test_a_file_swapped_for_a_link_after_lstat_is_not_read(step_log, tmp_path, monkeypatch) -> None:
    """The walk never reads a host file through a link that replaced a workspace file after its lstat."""
    workspace, session_dir, secret = tmp_path / "work", tmp_path / "session", tmp_path / "host-secret"
    workspace.mkdir()
    session_dir.mkdir()
    secret.write_text("host secret")
    (workspace / "data.txt").symlink_to(secret)
    _lstat_lies(monkeypatch, "data.txt", secret.stat())

    tree_id = _snapshot(TreeStore(step_log), workspace, session_dir).tree_id

    assert "data.txt" not in _tree_files(TreeStore(step_log), tree_id)


def test_a_name_that_is_not_utf8_is_left_out(step_log, tmp_path, monkeypatch) -> None:
    """``os.listdir`` gives such a name with surrogate escapes; the tree cannot encode it, so the walk skips it."""
    workspace, session_dir = tmp_path / "work", tmp_path / "session"
    workspace.mkdir()
    session_dir.mkdir()
    (workspace / "main.py").write_text("x = 1\n")
    bad = "bad\udcff"
    # APFS rejects such names, so the walk sees a fake link entry.
    real_listdir, real_readlink = os.listdir, os.readlink
    monkeypatch.setattr(
        os, "listdir", lambda path: [*real_listdir(path), bad] if isinstance(path, int) else real_listdir(path)
    )
    monkeypatch.setattr(
        os, "readlink", lambda path, *, dir_fd=None: "t" if path == bad else real_readlink(path, dir_fd=dir_fd)
    )
    link_info = list((workspace / "main.py").lstat())[:10]
    link_info[0] = stat.S_IFLNK | 0o777
    _lstat_lies(monkeypatch, bad, os.stat_result(link_info))

    tree_id = _snapshot(TreeStore(step_log), workspace, session_dir).tree_id

    assert set(_tree_files(TreeStore(step_log), tree_id)) == {"main.py"}


def test_a_pipe_that_looks_like_a_file_does_not_hang_the_snapshot(step_log, tmp_path, monkeypatch) -> None:
    """A FIFO in place of a file is opened without blocking and left out."""
    workspace, session_dir = tmp_path / "work", tmp_path / "session"
    workspace.mkdir()
    session_dir.mkdir()
    pipe = workspace / "pipe"
    os.mkfifo(pipe)
    _lstat_lies(monkeypatch, "pipe", _as_regular_file(pipe.stat()))
    result: list[str] = []
    walker = threading.Thread(
        target=lambda: result.append(_snapshot(TreeStore(step_log), workspace, session_dir).tree_id), daemon=True
    )
    walker.start()
    walker.join(timeout=10)
    try:
        assert not walker.is_alive()
    finally:
        if walker.is_alive():
            # A writer ends the blocked open, so the thread does not outlive the test.
            with contextlib.suppress(OSError):
                os.close(os.open(pipe, os.O_WRONLY | os.O_NONBLOCK))
    assert "pipe" not in _tree_files(TreeStore(step_log), result[0])


def test_restore_writes_the_workspace_and_session_folder_without_following_links(step_log, tmp_path) -> None:
    """A restore rebuilds files, modes and links, and replaces a link in the way instead of writing through it."""
    workspace, session_dir = tmp_path / "work", tmp_path / "home" / SESSIONS_DIR / "s1"
    (workspace / "src").mkdir(parents=True)
    session_dir.mkdir(parents=True)
    (workspace / "src" / "a.py").write_text("a = 1\n")
    (workspace / "run.sh").write_text("#!/bin/sh\n")
    (workspace / "run.sh").chmod(0o755)
    (workspace / "link").symlink_to("src/a.py")
    (session_dir / "native.json").write_text('{"turn": 3}')
    trees = TreeStore(step_log)
    tree_id = _snapshot(trees, workspace, session_dir).tree_id

    fresh, home = tmp_path / "fresh", tmp_path / "home2"
    outside = tmp_path / "outside"
    outside.mkdir()
    fresh.mkdir()
    (fresh / "src").symlink_to(outside)
    (fresh / "link").write_text("old")
    restore_session(trees, RUN, tree_id, fresh, home)

    assert (fresh / "src" / "a.py").read_text() == "a = 1\n"
    assert not (fresh / "src").is_symlink()
    assert list(outside.iterdir()) == []
    assert os.access(fresh / "run.sh", os.X_OK)
    assert not os.access(fresh / "src" / "a.py", os.X_OK)
    assert (fresh / "link").readlink() == Path("src/a.py")
    assert (home / SESSIONS_DIR / "s1" / "native.json").read_text() == '{"turn": 3}'
    with pytest.raises(ValueError, match="missing"):
        restore_session(trees, RUN, "f" * 64, fresh, home)


def test_restore_deletes_what_the_tree_does_not_name_without_following_links(step_log, tmp_path) -> None:
    """Files the agent deleted do not come back, and a link in the way is removed, not followed."""
    workspace, session_dir = tmp_path / "work", tmp_path / "home" / SESSIONS_DIR / "s1"
    (workspace / "src").mkdir(parents=True)
    session_dir.mkdir(parents=True)
    (workspace / "src" / "a.py").write_text("a = 1\n")
    tree_id = _snapshot(TreeStore(step_log), workspace, session_dir).tree_id

    fresh, home = tmp_path / "fresh", tmp_path / "home2"
    outside = tmp_path / "outside"
    (outside / "keep").mkdir(parents=True)
    (fresh / "src" / "gone").mkdir(parents=True)
    (fresh / "deleted.py").write_text("old")
    (fresh / "src" / "old.py").write_text("old")
    (fresh / "src" / "escape").symlink_to(outside)
    (home / SESSIONS_DIR / "other").mkdir(parents=True)
    restore_session(TreeStore(step_log), RUN, tree_id, fresh, home)

    assert sorted(path.relative_to(fresh).as_posix() for path in fresh.rglob("*")) == ["src", "src/a.py"]
    assert (outside / "keep").is_dir()
    assert (home / SESSIONS_DIR / "other").is_dir()


@pytest.mark.parametrize("inside", [False, True])
def test_a_workspace_entry_named_like_the_session_folder_is_left_out(step_log, tmp_path, inside) -> None:
    """The workspace's own session-folder entry does not break the snapshot; the real session folder fills the slot."""
    workspace = tmp_path / "work"
    session_dir = workspace / SESSIONS_DIR / "s1" if inside else tmp_path / "session"
    (workspace / SESSIONS_DIR / "x").mkdir(parents=True)
    session_dir.mkdir(parents=True, exist_ok=True)
    (workspace / SESSIONS_DIR / "x" / "f").write_text("x")
    (session_dir / "native.json").write_text("{}")

    tree_id = _snapshot(TreeStore(step_log), workspace, session_dir).tree_id

    assert set(_tree_files(TreeStore(step_log), tree_id)) == {f"{SESSIONS_DIR}/{session_dir.name}/native.json"}


def _bridge_env(tmp_path: Path, home: Path, proposer: dict[str, Any]) -> dict[str, str]:
    """Install the ``claude`` shim in ``home`` and return the environment that runs it.

    Args:
        tmp_path: Pytest's temporary folder.
        home: The sandbox home folder.
        proposer: The serialized launch.

    Returns:
        The process environment.
    """
    shim_dir = install_shim(home, Path(harness_bridge.__file__).resolve(), sys.executable)
    config = tmp_path / "proposer.json"
    config.write_text(json.dumps({**proposer, "model": "m1"}))
    return {
        **os.environ,
        "HOME": str(home),
        harness_bridge.CONFIG_ENV: str(config),
        "PATH": f"{shim_dir}{os.pathsep}{os.environ['PATH']}",
    }


def _claude(env: dict[str, str], workspace: Path, *args: str) -> str:
    """Run one ``claude --print`` call through the bridge and return its result text.

    Args:
        env: The process environment from :func:`_bridge_env`.
        workspace: The working directory.
        *args: Session and prompt arguments.

    Returns:
        The result document's ``result``.
    """
    done = subprocess.run(
        ["claude", "--print", "--output-format", "json", *args],
        cwd=workspace,
        env=env,
        capture_output=True,
        text=True,
        check=True,
        timeout=30,
    )
    return json.loads(done.stdout)["result"]


# Keeps its own state in the session folder and continues it on resume, as a harness with native resume does.
_NATIVE = {
    "run_command": 'echo started > "$APP_SESSION_DIR/native.txt"; cat "$APP_PROMPT_FILE"',
    "resume_command": 'echo "resumed $(cat "$APP_SESSION_DIR/native.txt")"; cat "$APP_PROMPT_FILE"',
    "output_format": "plain",
    "files": {},
    "env": {},
}
_FALLBACK = {"run_command": 'cat "$APP_PROMPT_FILE"', "output_format": "plain", "files": {}, "env": {}}


@pytest.mark.parametrize(("proposer", "native"), [(_NATIVE, True), (_FALLBACK, False)])
def test_a_resume_in_a_new_box_restores_the_session_folder_before_the_harness_starts(
    step_log, tmp_path, proposer, native
) -> None:
    """The committed tree carries the session folder: a native harness continues it, a fallback one gets the note."""
    home, workspace = tmp_path / "home", tmp_path / "work"
    workspace.mkdir()
    _claude(_bridge_env(tmp_path, home, proposer), workspace, "first task", "--session-id", "s1")
    trees = TreeStore(step_log)
    tree_id = snapshot_session(
        trees, RUN, workspace, home / SESSIONS_DIR / "s1", proposer=proposer, route_key=KEY
    ).tree_id

    new_home, new_workspace = tmp_path / "home-2", tmp_path / "work-2"
    new_workspace.mkdir()
    restore_session(trees, RUN, tree_id, new_workspace, new_home)
    result = _claude(_bridge_env(tmp_path, new_home, proposer), new_workspace, "--resume", "s1", "keep going")

    if native:
        assert result == "resumed started\nkeep going"
    else:
        assert result.startswith("first task")
        assert harness_bridge._RESUME_NOTE.strip() in result
        assert result.endswith("keep going")
