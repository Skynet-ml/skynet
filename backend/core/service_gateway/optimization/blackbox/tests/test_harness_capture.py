"""Tests for harness capture: tool call pairs and exec shim records as step commits, and the session tree."""

from __future__ import annotations

import json
import os
import subprocess
import sys
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

from .. import harness_bridge
from ..harness_bridge import SESSIONS_DIR, install_shim
from ..harness_capture import HarnessCapture, restore_session, snapshot_session

RUN = "job-ada"


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
        **kwargs: Extra capture arguments.

    Returns:
        The capture.
    """
    trees = TreeStore(step_log)
    return HarnessCapture(
        step_log,
        run_id=RUN,
        agent_id="proposer",
        execution_generation=1,
        grant_list_id="default",
        snapshot=lambda: snapshot_session(trees, RUN, workspace, session_dir),
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

    def snapshot() -> str:
        """Count each snapshot and take it.

        Returns:
            The tree id.
        """
        snapshots.append("taken")
        return snapshot_session(trees, RUN, workspace, session_dir)

    capture = HarnessCapture(
        step_log,
        run_id=RUN,
        agent_id="proposer",
        execution_generation=1,
        grant_list_id="default",
        snapshot=snapshot,
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
    log.write_text(
        json.dumps({"command": "make test", "exit_code": 2, "stdout": "FAIL\n", "stderr": "boom\n"})
        + "\nnot json\n[1]\n"
        + json.dumps({"command": "ls", "exit_code": 0, "stdout": "a\n", "stderr": ""})
        + "\n"
    )
    capture = _capture(step_log, workspace, session_dir, exec_log=log)

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


def test_the_snapshot_leaves_out_the_launch_files_that_hold_the_route_key(step_log, tmp_path) -> None:
    """The launch writes the real key into workspace files; those never reach the tree store."""
    workspace, session_dir = tmp_path / "work", tmp_path / "session"
    (workspace / ".codex").mkdir(parents=True)
    session_dir.mkdir()
    (workspace / ".codex" / "config.toml").write_text("key = real-key")
    (workspace / "main.py").write_text("x = 1\n")
    trees = TreeStore(step_log)

    tree_id = snapshot_session(trees, RUN, workspace, session_dir, exclude=[".codex/config.toml"])

    assert set(_tree_files(trees, tree_id)) == {"main.py"}


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
    tree_id = snapshot_session(trees, RUN, workspace, session_dir)

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


def test_a_workspace_entry_named_like_the_session_folder_is_refused(step_log, tmp_path) -> None:
    """A workspace that holds its own session-folder name cannot hide the real session folder."""
    workspace, session_dir = tmp_path / "work", tmp_path / "session"
    (workspace / SESSIONS_DIR).mkdir(parents=True)
    session_dir.mkdir()
    (workspace / SESSIONS_DIR / "x").write_text("x")

    with pytest.raises(ValueError, match="same name"):
        snapshot_session(TreeStore(step_log), RUN, workspace, session_dir)


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
    tree_id = snapshot_session(trees, RUN, workspace, home / SESSIONS_DIR / "s1")

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
