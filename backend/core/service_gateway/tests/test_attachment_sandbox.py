"""Tests for the agent chat's file workspace on the web app."""

from __future__ import annotations

import asyncio
import base64
import math
import shutil
import sys
import threading
import time
from collections.abc import Iterator
from pathlib import Path
from typing import Any

import pytest
from sqlalchemy import create_engine
from sqlalchemy.orm import Session

from core.billing.pricing import cents_for_usage
from core.billing.vercel_usage import sandbox_hold_cents
from core.sandbox.environment import ExecResult
from core.service_gateway.agents import attachment_sandbox as module
from core.service_gateway.agents.attachment_sandbox import (
    EXEC_COMMAND_TOOL,
    WRITE_STDIN_TOOL,
    ChatFile,
    SandboxCommandRunner,
    attachment_path,
    chat_file_loader,
    rewrite_paths,
)
from core.service_gateway.agents.generalist import _needs_approval, chat_rule_allowed
from core.storage.models import Base, ChatMediaModel

on_mac = pytest.mark.skipif(
    sys.platform != "darwin" or not Path("/usr/bin/sandbox-exec").exists(), reason="needs macOS sandbox-exec"
)


class FakeBox:
    """Keep a box's files in memory and run the copy command on them."""

    def __init__(self) -> None:
        """Start empty."""
        self.started = False
        self.stopped = False
        self.files: dict[str, str] = {}
        self.written: dict[str, bytes] = {}
        self.commands: list[str] = []
        self.gate: asyncio.Event | None = None
        self.dead = False

    async def start(self) -> None:
        """Mark the box as started."""
        self.started = True

    async def stop(self) -> None:
        """Mark the box as stopped."""
        self.stopped = True

    async def upload_files(self, files: dict[str, str]) -> None:
        """Keep the uploaded text files.

        Args:
            files: Maps paths to their text.
        """
        self.files.update(files)

    async def exec(self, command: str, *, timeout_sec: int = 0, on_output: Any = None, **_: Any) -> ExecResult:
        """Run the copy command, or echo the command.

        Args:
            command: The shell command.
            timeout_sec: Not used.
            on_output: Takes streamed output.

        Returns:
            The result.
        """
        self.commands.append(command)
        if "base64 -d" in command:
            media_id = command.split(".skynet-upload/")[1].split(".")[0]
            target = command.split("> ")[1].split(" &&")[0].strip("'")
            parts = sorted(k for k in self.files if k.startswith(f".skynet-upload/{media_id}."))
            self.written[target] = base64.b64decode("".join(self.files[k] for k in parts))
            return ExecResult(exit_code=0, stdout="", stderr="")
        if command.startswith("rm -rf "):
            folders = [part.strip("'") + "/" for part in command.split()[2:]]
            self.written = {k: v for k, v in self.written.items() if not k.startswith(tuple(folders))}
            return ExecResult(exit_code=0, stdout="", stderr="")
        if self.dead:
            # Failing after a short wait ends the future on the bridge loop, where its callback then runs.
            await asyncio.sleep(0.05)
            raise RuntimeError("box is gone")
        if self.gate is not None:
            await self.gate.wait()
        if on_output is not None:
            on_output("stdout", f"ran: {command}\n")
        return ExecResult(exit_code=0, stdout=f"ran: {command}\n", stderr="")


class Clock:
    """A clock the test moves by hand."""

    def __init__(self) -> None:
        """Start at zero."""
        self.now = 0.0

    def __call__(self) -> float:
        """Return the time now.

        Returns:
            The time.
        """
        return self.now


def make_runner(files: list[ChatFile], boxes: list[FakeBox], clock: Clock) -> SandboxCommandRunner:
    """Build a runner on fake boxes and a fixed file list.

    Args:
        files: The chat's files.
        boxes: Takes each box the runner opens.
        clock: The test clock.

    Returns:
        The runner.
    """

    def load(skip: set[str]) -> tuple[set[str], list[ChatFile]]:
        """Return the chat's ids and the files not in ``skip``.

        Args:
            skip: The held ids.

        Returns:
            The chat's ids, and the other files.
        """
        return {file.id for file in files}, [file for file in files if file.id not in skip]

    def factory(_conversation_id: str) -> Any:
        """Open a new fake box.

        Args:
            _conversation_id: Not used.

        Returns:
            The box.
        """
        box = FakeBox()
        boxes.append(box)
        return box

    return SandboxCommandRunner("chat-1", load, environment_factory=factory, clock=clock)


def test_attachment_path_and_rewrite() -> None:
    """Check the file path and that a copied file's path names its new id."""
    assert attachment_path("m1", "a b.csv") == "attachments/m1/a b.csv"
    assert attachment_path("m1", "..") == "attachments/m1/file"
    text = "path: attachments/old/a.csv and attachments/keep/b.csv"
    assert rewrite_paths(text, {"old": "new", "keep": "keep"}) == (
        "path: attachments/new/a.csv and attachments/keep/b.csv"
    )
    assert rewrite_paths(text, {}) == text


def test_box_opens_on_first_command_and_copies_files() -> None:
    """Check that the first command opens a box and copies the chat's files in."""
    files = [ChatFile("m1", "a.csv", b"x,y\n1,2\n")]
    boxes: list[FakeBox] = []
    runner = make_runner(files, boxes, Clock())
    assert boxes == []
    result = runner.exec_command("cat attachments/m1/a.csv")
    assert len(boxes) == 1
    assert boxes[0].started
    assert boxes[0].written == {"attachments/m1/a.csv": b"x,y\n1,2\n"}
    assert "Process exited with code 0" in result
    assert "ran: cat attachments/m1/a.csv" in result
    files.append(ChatFile("m2", "b.txt", b"hi"))
    runner.exec_command("ls")
    assert len(boxes) == 1
    assert set(boxes[0].written) == {"attachments/m1/a.csv", "attachments/m2/b.txt"}
    copies = [c for c in boxes[0].commands if "base64 -d" in c]
    assert len(copies) == 2
    runner.close()
    assert boxes[0].stopped


def test_file_that_left_the_chat_leaves_the_box() -> None:
    """Check that a deleted file is removed from the live box before the next command."""
    files = [ChatFile("m1", "a.txt", b"a"), ChatFile("m2", "b.txt", b"b")]
    boxes: list[FakeBox] = []
    runner = make_runner(files, boxes, Clock())
    runner.exec_command("true")
    del files[0]
    runner.exec_command("ls")
    assert set(boxes[0].written) == {"attachments/m2/b.txt"}
    assert "rm -rf attachments/m1" in boxes[0].commands
    runner.exec_command("ls")
    assert boxes[0].commands.count("rm -rf attachments/m1") == 1
    runner.close()


def test_file_that_left_the_chat_leaves_the_mac_workspace(tmp_path: Path) -> None:
    """Check that a deleted file is removed from the Mac workspace before the next command."""
    workspace = tmp_path / "ws"
    files = [ChatFile("m1", "a.txt", b"a"), ChatFile("m2", "b.txt", b"b")]
    runner = module._LocalFilesRunner(
        workspace, lambda skip: ({f.id for f in files}, [f for f in files if f.id not in skip])
    )
    runner.exec_command("true")
    assert (workspace / "attachments" / "m1" / "a.txt").exists()
    del files[0]
    runner.exec_command("true")
    assert not (workspace / "attachments" / "m1").exists()
    assert (workspace / "attachments" / "m2" / "b.txt").read_bytes() == b"b"
    runner.close()


def test_large_file_goes_in_parts(monkeypatch: pytest.MonkeyPatch) -> None:
    """Check that a file larger than one part arrives whole."""
    monkeypatch.setattr(module, "_UPLOAD_PART_CHARS", 8)
    data = bytes(range(256)) * 4
    boxes: list[FakeBox] = []
    runner = make_runner([ChatFile("m1", "blob.bin", data)], boxes, Clock())
    runner.exec_command("true")
    assert len([k for k in boxes[0].files if k.startswith(".skynet-upload/m1.")]) > 1
    assert boxes[0].written["attachments/m1/blob.bin"] == data
    runner.close()


def test_workdir_runs_in_folder_and_must_stay_inside() -> None:
    """Check that a workdir becomes a cd and that an outside workdir is refused."""
    boxes: list[FakeBox] = []
    runner = make_runner([], boxes, Clock())
    assert "must stay inside" in runner.exec_command("ls", workdir="../x")
    assert "must stay inside" in runner.exec_command("ls", workdir="/etc")
    assert boxes == []
    runner.exec_command("ls", workdir="attachments/m 1")
    assert boxes[0].commands[-1] == "cd -- 'attachments/m 1' && ls"
    runner.exec_command("ls", workdir="..data")
    assert boxes[0].commands[-1] == "cd -- ..data && ls"
    runner.close()


def test_running_command_gives_session_and_more_output() -> None:
    """Check that a slow command returns a session and that empty chars read on."""
    boxes: list[FakeBox] = []
    runner = make_runner([], boxes, Clock())
    runner.exec_command("true")
    gate = asyncio.Event()
    boxes[0].gate = gate
    first = runner.exec_command("sleep 5", yield_time_ms=50)
    assert "Process running with session ID" in first
    session_id = int(first.split("session ID ")[1].split()[0])
    refused = runner.write_stdin(session_id, chars="y\n")
    assert "cannot send input" in refused
    module.sync_bridge.run(_set, gate)
    second = runner.write_stdin(session_id, chars="", yield_time_ms=5000)
    assert "Process exited with code 0" in second
    assert "ran: sleep 5" in second
    assert "Unknown session" in runner.write_stdin(session_id, yield_time_ms=10)
    runner.close()


async def _set(event: asyncio.Event) -> None:
    """Set an event on the bridge's loop.

    Args:
        event: The event.
    """
    event.set()


def _cents(seconds: float) -> int:
    """Price a box's live time as the runner does.

    Args:
        seconds: The live time.

    Returns:
        The whole cents.
    """
    return math.ceil(sandbox_hold_cents(lifetime_ms=int(seconds * 1000), vcpus=2))


def test_charge_counts_live_time_once_per_box() -> None:
    """Check that each charge takes only the new cents and the creation cost once."""
    clock = Clock()
    boxes: list[FakeBox] = []
    runner = make_runner([], boxes, clock)
    assert runner.take_charge_cents() == 0
    runner.exec_command("true")
    clock.now = 600
    first = runner.take_charge_cents()
    assert first == _cents(600)
    clock.now = 1800
    second = runner.take_charge_cents()
    assert second == _cents(1800) - _cents(600)
    assert runner.take_charge_cents() == 0
    runner.owe_back(5)
    assert runner.take_charge_cents() == 5
    runner.close()


def test_box_reopens_near_its_end_and_keeps_old_cents() -> None:
    """Check that a box near its lifetime end closes and the next command opens a new one."""
    clock = Clock()
    boxes: list[FakeBox] = []
    files = [ChatFile("m1", "a.txt", b"a")]
    runner = make_runner(files, boxes, clock)
    lifetime = module.sandbox_lifetime_seconds()
    runner.exec_command("true")
    clock.now = lifetime - 60
    runner.exec_command("true")
    assert len(boxes) == 2
    assert boxes[0].stopped
    assert boxes[1].written == {"attachments/m1/a.txt": b"a"}
    clock.now = lifetime
    assert runner.take_charge_cents() == _cents(lifetime - 60) + _cents(60)
    runner.close()


def test_box_opens_again_before_a_command_could_outlive_it() -> None:
    """Check that a command never starts in a box that could end before the command timeout."""
    clock = Clock()
    boxes: list[FakeBox] = []
    runner = make_runner([], boxes, clock)
    lifetime = module.sandbox_lifetime_seconds()
    runner.exec_command("true")
    clock.now = lifetime - module.COMMAND_TIMEOUT_SECONDS + 1
    runner.exec_command("true")
    assert len(boxes) == 2
    runner.close()


def test_closed_runner_opens_no_new_box() -> None:
    """Check that a turn still running after its chat is deleted cannot open a box nobody stops."""
    boxes: list[FakeBox] = []
    runner = make_runner([ChatFile("m1", "a.txt", b"a")], boxes, Clock())
    runner.exec_command("true")
    runner.close()
    result = runner.exec_command("true")
    assert result.startswith("Error: this chat was deleted")
    assert len(boxes) == 1
    assert boxes[0].stopped


def test_closed_mac_runner_writes_no_files_again(tmp_path: Path) -> None:
    """Check that a Mac turn still running after its chat is deleted does not write the files back."""
    workspace = tmp_path / "ws"
    runner = module._LocalFilesRunner(workspace, lambda skip: ({"m1"}, [ChatFile("m1", "a.txt", b"a")]))
    runner.close()
    assert runner.exec_command("true").startswith("Error: this chat was deleted")
    assert not (workspace / "attachments").exists()


def test_file_loader_reads_one_file_at_a_time(tmp_path: Path) -> None:
    """Check that the loader yields only the chat owner's new files and reads their bytes lazily."""
    engine = create_engine(f"sqlite:///{tmp_path / 'media.db'}")
    Base.metadata.create_all(engine)
    with Session(engine) as session:
        for media_id, owner, chat in (("m1", "a@x.com", "c1"), ("m2", "a@x.com", "c1"), ("m3", "b@x.com", "c1")):
            session.add(
                ChatMediaModel(
                    id=media_id,
                    username=owner,
                    conversation_id=chat,
                    filename=f"{media_id}.txt",
                    kind="document",
                    mime_type="text/plain",
                    byte_size=2,
                    data=media_id.encode(),
                )
            )
        session.commit()
    held, files = chat_file_loader(engine, "a@x.com", "c1")({"m1"})
    assert held == {"m1", "m2"}
    assert isinstance(files, Iterator)
    assert [(f.id, f.name, f.data) for f in files] == [("m2", "m2.txt", b"m2")]
    engine.dispose()


def test_dead_box_reopens_and_keeps_its_cents() -> None:
    """Check that a box that stopped early is dropped, and the next command opens a new one."""
    clock = Clock()
    boxes: list[FakeBox] = []
    files = [ChatFile("m1", "a.txt", b"a")]
    runner = make_runner(files, boxes, clock)
    runner.exec_command("true")
    clock.now = 300
    boxes[0].dead = True
    assert "The command failed in the workspace" in runner.exec_command("true")
    deadline = time.monotonic() + 5
    while not boxes[0].stopped and time.monotonic() < deadline:
        time.sleep(0.01)
    assert boxes[0].stopped
    clock.now = 900
    assert "ran: true" in runner.exec_command("true")
    assert len(boxes) == 2
    assert boxes[1].written == {"attachments/m1/a.txt": b"a"}
    clock.now = 1000
    assert runner.take_charge_cents() == _cents(300) + _cents(100)
    runner.close()


def test_file_write_refuses_a_link_out_of_the_workspace(tmp_path: Path) -> None:
    """Check that a link a command put in the workspace cannot move a file write outside it."""
    workspace, outside = tmp_path / "ws", tmp_path / "outside"
    workspace.mkdir()
    outside.mkdir()
    (workspace / "attachments").symlink_to(outside)
    with pytest.raises(OSError):
        module._write_inside(workspace, "attachments/m1/a.txt", b"x")
    assert not any(outside.iterdir())
    (workspace / "attachments").unlink()
    module._write_inside(workspace, "attachments/m1/a.txt", b"x")
    assert (workspace / "attachments" / "m1" / "a.txt").read_bytes() == b"x"


def test_file_write_replaces_a_hard_link_and_keeps_its_target(tmp_path: Path) -> None:
    """Check that a hard link a command put at a file's path never lets the write reach the linked file."""
    workspace, outside = tmp_path / "ws", tmp_path / "secret.txt"
    (workspace / "attachments" / "m1").mkdir(parents=True)
    outside.write_bytes(b"keep")
    (workspace / "attachments" / "m1" / "a.txt").hardlink_to(outside)
    module._write_inside(workspace, "attachments/m1/a.txt", b"new")
    assert outside.read_bytes() == b"keep"
    assert (workspace / "attachments" / "m1" / "a.txt").read_bytes() == b"new"
    assert sorted(p.name for p in (workspace / "attachments" / "m1").iterdir()) == ["a.txt"]


def test_stop_chat_runner_deletes_a_mac_workspace_with_no_runner(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    """Check that a Mac chat deleted after an app restart still loses its workspace folder."""
    monkeypatch.setattr(module.settings, "local_mode", True)
    monkeypatch.setattr(module.settings, "local_state_dir", str(tmp_path))
    folder = module.local_workspace("chat-1") / "attachments" / "m1"
    folder.mkdir(parents=True)
    (folder / "a.txt").write_bytes(b"a")
    module.stop_chat_runner("chat-1").join(timeout=10)
    assert not module.local_workspace("chat-1").exists()


def test_web_runner_unused_for_a_lifetime_is_closed(monkeypatch: pytest.MonkeyPatch) -> None:
    """Check that a web chat's runner leaves memory and stops its box after a whole idle lifetime."""
    monkeypatch.setattr(module.settings, "local_mode", False)
    monkeypatch.setattr(module, "_RUNNERS", {})
    monkeypatch.setattr(module, "_RUNNERS_USED", {})
    boxes: list[FakeBox] = []
    old = make_runner([], boxes, Clock())
    old.exec_command("true")
    module._RUNNERS["old"] = old
    module._RUNNERS_USED["old"] = time.monotonic() - module.sandbox_lifetime_seconds() - 1
    module._RUNNERS["recent"] = recent = make_runner([], [], Clock())
    module._RUNNERS_USED["recent"] = time.monotonic()
    module.chat_runner(None, "alice@example.com", "new")
    assert set(module._RUNNERS) == {"recent", "new"}
    assert module._RUNNERS["recent"] is recent
    deadline = time.monotonic() + 5
    while not boxes[0].stopped and time.monotonic() < deadline:
        time.sleep(0.01)
    assert boxes[0].stopped


def test_stop_chat_runner_closes_the_box(monkeypatch: pytest.MonkeyPatch) -> None:
    """Check that stopping a chat's runner stops its box and forgets it."""
    boxes: list[FakeBox] = []
    runner = make_runner([], boxes, Clock())
    runner.exec_command("true")
    monkeypatch.setitem(module._RUNNERS, "chat-1", runner)
    module.stop_chat_runner("chat-1").join(timeout=10)
    assert "chat-1" not in module._RUNNERS
    assert boxes[0].stopped


def test_a_chat_deleted_mid_turn_still_charges_the_turn(monkeypatch: pytest.MonkeyPatch) -> None:
    """Check that a box closed by a chat delete keeps its live time for the turn's charge."""
    clock = Clock()
    runner = make_runner([], [], clock)
    runner.exec_command("true")
    clock.now = 600
    monkeypatch.setitem(module._RUNNERS, "chat-1", runner)
    module.stop_chat_runner("chat-1").join(timeout=10)
    clock.now = 900
    assert runner.take_charge_cents() == _cents(600)
    assert runner.take_charge_cents() == 0


def test_charge_debits_exact_cents(monkeypatch: pytest.MonkeyPatch) -> None:
    """Check that the debit asks for the owed cents and gives them back on failure."""
    clock = Clock()
    runner = make_runner([], [], clock)
    runner.exec_command("true")
    clock.now = 600
    calls: list[dict[str, Any]] = []

    class Billing:
        """Record a debit and price it with the billing code that ``debit_run`` uses."""

        def __init__(self, engine: Any) -> None:
            """Keep nothing.

            Args:
                engine: Not used.
            """

        def debit_run(self, username: str, usages: list[Any], **kwargs: Any) -> int:
            """Record the debit.

            Args:
                username: The owner.
                usages: The usages.
                **kwargs: The debit options.

            Returns:
                The charged cents.
            """
            calls.append({"username": username, **kwargs})
            return cents_for_usage(usages)

    monkeypatch.setattr(module, "StripeBillingService", Billing)
    charged = module.charge_chat_sandbox(object(), "ann", runner, settlement_key="k1")
    assert charged == _cents(600)
    for owed in (1, 2, 7, 99, 1000, 123457):
        monkeypatch.setattr(runner, "take_charge_cents", lambda owed=owed: owed)
        assert module.charge_chat_sandbox(object(), "ann", runner, settlement_key="k") == owed
    monkeypatch.undo()
    monkeypatch.setattr(module, "StripeBillingService", Billing)
    assert calls[0]["settlement_key"] == "k1"
    assert calls[0]["token_source"] == "managed"

    class Broken(Billing):
        """Fail every debit."""

        def debit_run(self, username: str, usages: list[Any], **kwargs: Any) -> int:
            """Fail.

            Args:
                username: The owner.
                usages: The usages.
                **kwargs: The debit options.

            Raises:
                RuntimeError: Always.
            """
            raise RuntimeError("down")

    clock.now = 1200
    monkeypatch.setattr(module, "StripeBillingService", Broken)
    assert module.charge_chat_sandbox(object(), "ann", runner, settlement_key="k2") == 0
    assert runner.take_charge_cents() == _cents(1200) - _cents(600)
    runner.close()


def test_commands_ask_only_in_ask_mode() -> None:
    """Check that a command asks first in Ask mode, and its write_stdin calls never ask."""
    assert _needs_approval(EXEC_COMMAND_TOOL, "ask") is True
    assert chat_rule_allowed(EXEC_COMMAND_TOOL) is True
    for mode in ("ask", "auto_safe", "yolo"):
        assert _needs_approval(WRITE_STDIN_TOOL, mode) is False
    for mode in ("auto_safe", "yolo"):
        assert _needs_approval(EXEC_COMMAND_TOOL, mode) is False


def test_failed_start_stops_the_box() -> None:
    """Check that a box whose start fails is stopped, and the next command opens a new one."""
    boxes: list[FakeBox] = []
    runner = make_runner([], boxes, Clock())

    async def broken_start() -> None:
        """Fail like a provider that made the box but timed out."""
        raise RuntimeError("start timed out")

    def factory(_conversation_id: str) -> Any:
        """Open a fake box whose first start fails.

        Args:
            _conversation_id: Not used.

        Returns:
            The box.
        """
        box = FakeBox()
        if not boxes:
            box.start = broken_start  # type: ignore[method-assign]
        boxes.append(box)
        return box

    runner._factory = factory
    assert runner.exec_command("true").startswith("Error: the file workspace did not open")
    assert boxes[0].stopped
    assert "ran: true" in runner.exec_command("true")
    assert len(boxes) == 2
    runner.close()


def test_close_before_the_command_is_sent_sends_nothing(monkeypatch: pytest.MonkeyPatch) -> None:
    """Check that a chat deleted after the box is ready but before the command goes gets no command."""
    boxes: list[FakeBox] = []
    runner = make_runner([], boxes, Clock())
    open_session = runner.open

    def open_then_close() -> Any:
        """Open the session, then close the runner as a chat delete would.

        Returns:
            The session.
        """
        session = open_session()
        runner.close()
        return session

    monkeypatch.setattr(runner, "open", open_then_close)
    result = runner.exec_command("echo hi")
    assert "the file workspace closed before the command started" in result
    assert "echo hi" not in boxes[0].commands
    assert boxes[0].stopped


def test_mac_removal_never_leaves_the_workspace(tmp_path: Path) -> None:
    """Check that a link in place of the attachments folder keeps a removal inside the workspace."""
    workspace = tmp_path / "ws"
    outside = tmp_path / "outside"
    files = [ChatFile("m1", "a.txt", b"a")]
    runner = module._LocalFilesRunner(
        workspace, lambda skip: ({f.id for f in files}, [f for f in files if f.id not in skip])
    )
    runner.exec_command("true")
    (outside / "m1").mkdir(parents=True)
    shutil.rmtree(workspace / "attachments")
    (workspace / "attachments").symlink_to(outside)
    files.clear()
    runner.exec_command("true")
    assert (outside / "m1").is_dir()
    runner.close()


@on_mac
def test_mac_close_during_the_file_copy_ends_the_new_command(tmp_path: Path) -> None:
    """Check that a chat deleted while files are copied still ends the command that then starts."""
    workspace = tmp_path / "ws"
    closer: list[threading.Thread] = []

    def load(skip: set[str]) -> tuple[set[str], list[ChatFile]]:
        """Start a chat delete in another thread while the files are copied.

        Args:
            skip: Not used.

        Returns:
            One file.
        """
        thread = threading.Thread(target=runner.close)
        thread.start()
        closer.append(thread)
        time.sleep(0.1)
        return {"m1"}, [ChatFile("m1", "a.txt", b"a")]

    runner = module._LocalFilesRunner(workspace, load)
    started: list[Any] = []
    start = runner.start

    def record(cmd: str, workdir: str | None = None) -> Any:
        """Start the command and keep its session.

        Args:
            cmd: The shell command.
            workdir: The directory to run in.

        Returns:
            The session.
        """
        started.append(start(cmd, workdir))
        return started[-1]

    runner.start = record  # type: ignore[method-assign]
    runner.exec_command("sleep 30", yield_time_ms=250)
    closer[0].join(timeout=10)
    assert not closer[0].is_alive()
    assert started[0].process.wait(timeout=5) != 0


def test_a_file_that_cannot_be_copied_leaves_the_others_working() -> None:
    """Check that one failed copy skips that file, and commands still run on the other files."""
    files = [ChatFile("bad", "x" * 300, b"a"), ChatFile("m2", "b.txt", b"b")]
    boxes: list[FakeBox] = []
    runner = make_runner(files, boxes, Clock())
    put = runner._put
    tried: list[str] = []

    def put_or_fail(env: Any, file: ChatFile) -> None:
        """Fail on the long name, as a file system does.

        Args:
            env: The box.
            file: The file.

        Raises:
            RuntimeError: For the long name.
        """
        tried.append(file.id)
        if file.id == "bad":
            raise RuntimeError("File name too long")
        put(env, file)

    runner._put = put_or_fail  # type: ignore[method-assign]
    assert "ran: ls" in runner.exec_command("ls")
    assert set(boxes[0].written) == {"attachments/m2/b.txt"}
    assert "rm -rf .skynet-upload/bad.* attachments/bad" in boxes[0].commands
    runner.exec_command("ls")
    assert tried == ["bad", "m2"]
    runner.close()


def test_a_failed_removal_closes_the_box() -> None:
    """Check that a file that left the chat but stays in the box closes that box, and the next box lacks it."""
    files = [ChatFile("m1", "a.txt", b"a"), ChatFile("m2", "b.txt", b"b")]
    boxes: list[FakeBox] = []
    runner = make_runner(files, boxes, Clock())
    runner.exec_command("true")
    exec_ = boxes[0].exec

    async def fail_rm(command: str, **kwargs: Any) -> ExecResult:
        """Fail the removal, and run any other command.

        Args:
            command: The shell command.
            **kwargs: The other arguments.

        Returns:
            The result.
        """
        if command.startswith("rm -rf attachments/"):
            return ExecResult(exit_code=1, stdout="", stderr="busy")
        return await exec_(command, **kwargs)

    boxes[0].exec = fail_rm  # type: ignore[method-assign]
    del files[0]
    assert "did not open" in runner.exec_command("ls")
    assert boxes[0].stopped
    assert "ran: ls" in runner.exec_command("ls")
    assert set(boxes[1].written) == {"attachments/m2/b.txt"}
    runner.close()


def test_mac_restart_writes_a_file_whose_folder_is_empty(tmp_path: Path) -> None:
    """Check that an empty file folder left by a failed write does not count as a copied file."""
    workspace = tmp_path / "ws"
    (workspace / "attachments" / "m1").mkdir(parents=True)
    files = [ChatFile("m1", "a.txt", b"a")]
    runner = module._LocalFilesRunner(
        workspace, lambda skip: ({f.id for f in files}, [f for f in files if f.id not in skip])
    )
    runner.exec_command("true")
    assert (workspace / "attachments" / "m1" / "a.txt").read_bytes() == b"a"
    runner.close()


def test_a_running_command_keeps_its_box_until_near_its_end() -> None:
    """Check that a reopen waits for a running command, unless the box is near its end."""
    clock = Clock()
    boxes: list[FakeBox] = []
    runner = make_runner([], boxes, clock)
    runner.exec_command("true")
    boxes[0].gate = asyncio.Event()
    runner.exec_command("sleep 600", yield_time_ms=0)
    clock.now = runner._reopen_after
    runner.exec_command("ls", yield_time_ms=0)
    assert len(boxes) == 1
    clock.now = runner._reopen_by
    runner.exec_command("ls", yield_time_ms=0)
    assert len(boxes) == 2
    assert boxes[0].stopped
    runner.close()


def test_mac_restart_still_removes_files_that_left_the_chat(tmp_path: Path) -> None:
    """Check that a file copied before a restart leaves the workspace when it leaves the chat."""
    workspace = tmp_path / "ws"
    files = [ChatFile("m1", "a.txt", b"a")]

    def load(skip: set[str]) -> tuple[set[str], list[ChatFile]]:
        """Return the chat's ids and its other files.

        Args:
            skip: The held ids.

        Returns:
            The ids, and the other files.
        """
        return {f.id for f in files}, [f for f in files if f.id not in skip]

    module._LocalFilesRunner(workspace, load).exec_command("true")
    assert (workspace / "attachments" / "m1" / "a.txt").exists()
    files.clear()
    module._LocalFilesRunner(workspace, load).exec_command("true")
    assert not (workspace / "attachments" / "m1").exists()
