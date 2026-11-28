"""Tests for the setup agent's private copy of a local folder (SKY-997)."""

from __future__ import annotations

import hashlib
import json
import os
import re
import subprocess
import sys
import threading
import time
from collections.abc import Iterator
from pathlib import Path
from types import SimpleNamespace
from typing import Any

import pytest

from core.config import settings
from core.local.folder_tree import FolderTreeError, file_digest, folder_entries
from core.local.picked_folders import PICKED_FOLDERS_FILE
from core.service_gateway.agents import folder_workspace as fw
from core.service_gateway.agents.apply_patch import apply_patch
from core.service_gateway.agents.attachment_sandbox import stop_chat_runner
from core.service_gateway.agents.folder_workspace import (
    FolderWorkspace,
    close_workspace,
    live_workspace,
    open_workspace,
    saved_setup_secrets,
    session_folder,
    sweep_stale_copies,
)
from core.service_gateway.agents.repo_browser import LocalFolderBrowser

on_mac = pytest.mark.skipif(
    sys.platform != "darwin" or not Path("/usr/bin/sandbox-exec").exists(), reason="needs macOS sandbox-exec"
)


def tree_hash(root: Path) -> str:
    """Hash every entry below a folder: names, kinds, modes, bytes and link targets.

    Args:
        root: The folder.

    Returns:
        The hex digest.
    """
    digest = hashlib.sha256()
    for path in sorted(root.rglob("*")):
        info = os.lstat(path)
        digest.update(f"{path.relative_to(root)}\0{info.st_mode}\0{info.st_mtime_ns}\0".encode())
        if path.is_symlink():
            digest.update(str(path.readlink()).encode())
        elif path.is_file():
            digest.update(path.read_bytes())
    return digest.hexdigest()


def pick(state: Path, *folders: Path | str) -> None:
    """Write the picked-folder list as the desktop app does.

    Args:
        state: The app's state folder.
        folders: The picked folders.
    """
    state.mkdir(parents=True, exist_ok=True)
    (state / PICKED_FOLDERS_FILE).write_text(json.dumps([str(f) for f in folders]), encoding="utf-8")


@pytest.fixture
def state_dir(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Iterator[Path]:
    """Point the app's state folder at a test folder and empty the workspace registry.

    Args:
        tmp_path: The test's folder.
        monkeypatch: The patcher.

    Yields:
        The state folder.
    """
    state = tmp_path / "state"
    monkeypatch.setattr(settings, "local_state_dir", str(state))
    monkeypatch.setattr(settings, "local_mode", True)
    monkeypatch.setattr(fw, "_WORKSPACES", {})
    monkeypatch.setattr(fw, "_KEY_LOCKS", {})
    monkeypatch.setattr(fw, "_ABORTED", set())
    yield state
    for key in list(fw._WORKSPACES):
        close_workspace(key)


@pytest.fixture
def source(tmp_path: Path, state_dir: Path) -> Path:
    """Make a picked user folder with files that must and must not reach the copy.

    Args:
        tmp_path: The test's folder.
        state_dir: The app's state folder, which gets the folder as picked.

    Returns:
        The folder.
    """
    root = tmp_path / "project"
    (root / "src").mkdir(parents=True)
    (root / "src" / "app.py").write_text("def main():\n    return 1\n")
    (root / "run.sh").write_text("echo hi\n")
    (root / "run.sh").chmod(0o755)
    (root / "README.md").write_text("# Project\n")
    (root / ".gitignore").write_text("build/\n*.log\n")
    (root / ".env").write_text("TOKEN=secret\n")
    (root / "config").mkdir()
    (root / "config" / ".env.local").write_text("TOKEN=secret\n")
    (root / "node_modules" / "pkg").mkdir(parents=True)
    (root / "node_modules" / "pkg" / "index.js").write_text("x")
    (root / "build").mkdir()
    (root / "build" / "out.bin").write_bytes(b"\0")
    (root / "debug.log").write_text("log")
    (root / ".git").mkdir()
    (root / ".git" / "HEAD").write_text("ref: refs/heads/main\n")
    outside = tmp_path / "outside"
    outside.mkdir()
    (outside / "secret.txt").write_text("outside\n")
    (root / "linked_dir").symlink_to(outside, target_is_directory=True)
    (root / "linked_file.txt").symlink_to(outside / "secret.txt")
    pick(state_dir, root)
    return root


def test_the_copy_holds_only_the_listed_files(state_dir: Path, source: Path) -> None:
    workspace, new = open_workspace("chat1", source)
    assert new
    copied = sorted(str(path.relative_to(workspace.folder)) for path in workspace.folder.rglob("*"))
    assert copied == [".gitignore", "README.md", "config", "run.sh", "src", "src/app.py"]
    assert not any(path.is_symlink() for path in workspace.folder.rglob("*"))
    assert (workspace.folder / "run.sh").stat().st_mode & 0o111
    # The copy sits in the app's state folder, not in the user's folder.
    assert workspace.folder == session_folder("chat1") / "folder"
    assert state_dir in workspace.folder.parents
    # The same id the folder browser gives, so stored draft bases compare.
    assert workspace.base == LocalFolderBrowser(str(source)).branch


def test_the_users_folder_never_changes(state_dir: Path, source: Path, tmp_path: Path) -> None:
    before = tree_hash(source)
    outside_before = tree_hash(tmp_path / "outside")
    workspace, _ = open_workspace("chat1", source)
    apply_patch(
        workspace.folder,
        "*** Begin Patch\n*** Update File: src/app.py\n@@\n-    return 1\n+    return 2\n"
        "*** Delete File: README.md\n*** Add File: new.txt\n+new\n*** End Patch",
    )
    workspace.write("metric.py", "def score(repo_path):\n    return 1\n")
    workspace.changes()
    workspace.reset({}, [])
    close_workspace("chat1")
    assert tree_hash(source) == before
    assert tree_hash(tmp_path / "outside") == outside_before


@on_mac
def test_a_command_cannot_write_the_users_folder(state_dir: Path, source: Path) -> None:
    before = tree_hash(source)
    workspace, _ = open_workspace("chat1", source)
    result = workspace.exec_command(f"echo x > {source}/src/app.py; echo written=$?; rm -f {source}/README.md")
    assert "written=1" in result
    close_workspace("chat1")
    assert tree_hash(source) == before


def test_changes_report_edits_and_deletes_and_skip_build_output(state_dir: Path, source: Path) -> None:
    workspace, _ = open_workspace("chat1", source)
    apply_patch(
        workspace.folder,
        "*** Begin Patch\n*** Update File: src/app.py\n@@\n-    return 1\n+    return 2\n"
        "*** Add File: tests/test_app.py\n+def test_main():\n+    pass\n*** Delete File: README.md\n*** End Patch",
    )
    for folder in ("node_modules/x", ".venv/lib", "src/__pycache__", "build"):
        (workspace.folder / folder).mkdir(parents=True, exist_ok=True)
        (workspace.folder / folder / "file.py").write_text("generated\n")
    (workspace.folder / "data.bin").write_bytes(b"\xff\xfe\x00")
    (workspace.folder / "big.txt").write_text("x" * (fw.DRAFT_FILE_BYTES + 1))
    found = workspace.changes()
    assert found.files == {
        "src/app.py": "def main():\n    return 2\n",
        "tests/test_app.py": "def test_main():\n    pass\n",
    }
    assert found.deleted == ["README.md"]
    assert set(found.skipped) == {"data.bin", "big.txt"}
    assert "UTF-8" in found.skipped["data.bin"]


def test_a_tracked_draft_that_matches_the_folder_again_comes_back(state_dir: Path, source: Path) -> None:
    workspace, _ = open_workspace("chat1", source)
    found = workspace.changes(["src/app.py", "missing.py"])
    assert found.files == {"src/app.py": "def main():\n    return 1\n"}


def test_reset_puts_the_draft_on_the_folder_and_drops_other_changes(state_dir: Path, source: Path) -> None:
    workspace, _ = open_workspace("chat1", source)
    (workspace.folder / "src" / "app.py").write_text("discarded\n")
    (workspace.folder / "scratch.txt").write_text("not a draft\n")
    (workspace.folder / "run.sh").unlink()
    workspace.reset({"README.md": "# Draft\n", "metric.py": "m\n"}, ["src/app.py"])
    assert (workspace.folder / "README.md").read_text() == "# Draft\n"
    assert (workspace.folder / "metric.py").read_text() == "m\n"
    assert not (workspace.folder / "src" / "app.py").exists()
    assert not (workspace.folder / "scratch.txt").exists()
    assert (workspace.folder / "run.sh").read_text() == "echo hi\n"
    assert (workspace.folder / "run.sh").stat().st_mode & 0o111


def test_the_workspace_is_reused_until_the_folder_changes(state_dir: Path, source: Path) -> None:
    first, _ = open_workspace("chat1", source)
    again, new = open_workspace("chat1", source)
    assert again is first
    assert not new
    (source / "src" / "app.py").write_text("changed by the user\n")
    third, new = open_workspace("chat1", source)
    assert new
    assert third is not first
    assert first.closed
    assert (third.folder / "src" / "app.py").read_text() == "changed by the user\n"
    assert third.base != first.base


def test_close_deletes_the_copy(state_dir: Path, source: Path) -> None:
    workspace, _ = open_workspace("chat1", source)
    assert workspace.folder.exists()
    close_workspace("chat1")
    assert not session_folder("chat1").exists()
    assert live_workspace("chat1") is None
    assert "closed" in workspace.exec_command("echo hi")


def test_close_without_an_open_workspace_deletes_a_stale_copy(state_dir: Path) -> None:
    stale = session_folder("chat2") / "folder"
    stale.mkdir(parents=True)
    close_workspace("chat2")
    assert not session_folder("chat2").exists()


def test_the_sweep_deletes_copies_and_proxy_state_an_earlier_process_left(state_dir: Path) -> None:
    stale = session_folder("old-chat") / "folder"
    stale.mkdir(parents=True)
    proxy = state_dir / fw.EGRESS_DIR / "egress-abc123"
    proxy.mkdir(parents=True)
    (proxy / "key.pem").write_text("k")
    other = state_dir / fw.EGRESS_DIR / "keep.txt"
    other.write_text("x")
    sweep_stale_copies()
    assert not stale.exists()
    assert not proxy.exists()
    assert other.exists()


@pytest.mark.parametrize("key", ["../x", "a/b", "", "a" * 129])
def test_an_unsafe_chat_id_is_refused(state_dir: Path, key: str) -> None:
    with pytest.raises(ValueError):
        session_folder(key)


def test_a_folder_that_is_a_link_is_refused(state_dir: Path, source: Path, tmp_path: Path) -> None:
    link = tmp_path / "link"
    link.symlink_to(source, target_is_directory=True)
    with pytest.raises(fw.FolderTreeError):
        FolderWorkspace(link, tmp_path / "session", tmp_path / "egress")
    assert not (tmp_path / "session").exists()


def test_a_folder_the_user_did_not_pick_is_refused_with_no_copy(
    state_dir: Path, source: Path, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """A folder off the picked list is refused before it is listed or copied (SKY-1013)."""
    calls: list[str] = []
    monkeypatch.setattr(fw, "folder_entries", lambda *args, **kwargs: calls.append("list") or ([], False))
    monkeypatch.setattr(fw, "copy_folder", lambda *args, **kwargs: calls.append("copy") or {})
    pick(state_dir, tmp_path / "elsewhere")
    with pytest.raises(FolderTreeError, match=f"^{re.escape(fw.NOT_PICKED)}$"):
        open_workspace("chat1", source)
    assert calls == []
    assert live_workspace("chat1") is None
    assert not session_folder("chat1").exists()


def test_an_open_copy_is_not_reused_once_its_folder_is_off_the_picked_list(
    state_dir: Path, source: Path, tmp_path: Path
) -> None:
    """The picked check also runs before an open copy is reused."""
    open_workspace("chat1", source)
    pick(state_dir, tmp_path / "elsewhere")
    with pytest.raises(FolderTreeError, match=f"^{re.escape(fw.NOT_PICKED)}$"):
        open_workspace("chat1", source)


def test_no_picked_list_refuses_every_folder(state_dir: Path, source: Path) -> None:
    """Without a picked-folder list, no folder opens."""
    (state_dir / PICKED_FOLDERS_FILE).unlink()
    with pytest.raises(FolderTreeError, match=f"^{re.escape(fw.NOT_PICKED)}$"):
        open_workspace("chat1", source)
    assert not session_folder("chat1").exists()


def test_a_folder_below_a_picked_folder_opens(state_dir: Path, source: Path) -> None:
    """A folder inside a picked folder opens as before."""
    workspace, new = open_workspace("chat1", source / "src")
    assert new
    assert (workspace.folder / "app.py").read_text() == "def main():\n    return 1\n"


def test_a_dot_dot_path_out_of_the_picked_folder_is_refused(state_dir: Path, source: Path) -> None:
    """A ``..`` path that leaves the picked folder is refused."""
    with pytest.raises(FolderTreeError, match=f"^{re.escape(fw.NOT_PICKED)}$"):
        open_workspace("chat1", f"{source}/src/../../outside")
    assert not session_folder("chat1").exists()


def test_a_path_through_a_link_to_a_folder_outside_is_refused(state_dir: Path, source: Path, tmp_path: Path) -> None:
    """A path through a link in the picked folder to a folder outside it is refused."""
    (tmp_path / "outside" / "inner").mkdir()
    with pytest.raises(FolderTreeError, match=f"^{re.escape(fw.NOT_PICKED)}$"):
        open_workspace("chat1", source / "linked_dir" / "inner")
    assert not session_folder("chat1").exists()


@on_mac
def test_a_shell_edit_reaches_the_changes(state_dir: Path, source: Path) -> None:
    workspace, _ = open_workspace("chat1", source)
    result = workspace.exec_command("printf 'def main():\\n    return 3\\n' > src/app.py && rm README.md && pwd")
    assert str(workspace.folder.resolve()) in result
    found = workspace.changes()
    assert found.files == {"src/app.py": "def main():\n    return 3\n"}
    assert found.deleted == ["README.md"]
    # HOME and the temporary folder sit beside the copy, so tool caches never become drafts.
    assert not (workspace.folder / ".tmp").exists()


@on_mac
def test_stop_ends_a_running_command(state_dir: Path, source: Path) -> None:
    marker = f"sleep {400 + os.getpid() % 97}"
    workspace, _ = open_workspace("chat1", source)
    first = workspace.exec_command(marker, None, 200)
    assert "session ID" in first
    workspace.stop_commands()
    deadline = time.monotonic() + 5
    while time.monotonic() < deadline:
        if subprocess.run(["pgrep", "-f", marker], capture_output=True, check=False).returncode != 0:
            break
        time.sleep(0.05)
    assert subprocess.run(["pgrep", "-f", marker], capture_output=True, check=False).returncode != 0


def test_only_saved_secrets_with_hosts_reach_the_proxy(monkeypatch: pytest.MonkeyPatch) -> None:
    views = [
        SimpleNamespace(id="1", name="API_KEY", allowed_hosts=["api.example.com"]),
        SimpleNamespace(id="2", name="PLAIN", allowed_hosts=[]),
        SimpleNamespace(id="3", name="BROKEN", allowed_hosts=["x.example.com"]),
    ]

    class Store:
        """A saved-secret store with fixed secrets."""

        def __init__(self, engine: Any) -> None:
            """Keep nothing.

            Args:
                engine: Unused.
            """

        def list(self, username: str) -> list[SimpleNamespace]:
            """Return the views.

            Args:
                username: Unused.

            Returns:
                The views.
            """
            return views

        def resolve(self, username: str, secret_id: str, hosts: Any) -> str:
            """Return a value, or fail for the broken secret.

            Args:
                username: Unused.
                secret_id: The secret.
                hosts: Unused.

            Returns:
                The value.

            Raises:
                RuntimeError: For the broken secret.
            """
            if secret_id == "3":
                raise RuntimeError("cannot decrypt")
            return f"value-{secret_id}"

    monkeypatch.setattr(fw, "SavedSecretStore", Store)
    secrets = saved_setup_secrets(object(), "alice")
    assert [(item.name, item.value, item.allowed_hosts) for item in secrets] == [
        ("API_KEY", "value-1", ("api.example.com",))
    ]


def test_a_slow_copy_of_one_chat_never_holds_up_another_chat(
    state_dir: Path, source: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """While one chat's folder copies, another chat's lookup and open return at once."""
    started = threading.Event()
    release = threading.Event()
    real_copy = fw.copy_folder

    def slow_copy(*args: Any) -> dict[str, bytes]:
        """Wait for the test before the first copy.

        Args:
            args: The :func:`copy_folder` arguments.

        Returns:
            The copy's snapshot.
        """
        if not started.is_set():
            started.set()
            assert release.wait(10)
        return real_copy(*args)

    monkeypatch.setattr(fw, "copy_folder", slow_copy)
    slow = threading.Thread(target=open_workspace, args=("slow", source))
    slow.start()
    try:
        assert started.wait(5)
        begin = time.monotonic()
        assert live_workspace("slow") is None
        other, new = open_workspace("other", source)
        assert new
        assert time.monotonic() - begin < 5
        assert other.folder.exists()
    finally:
        release.set()
        slow.join(10)
    assert live_workspace("slow") is not None


def test_a_chat_closed_while_its_folder_copies_keeps_no_copy(
    state_dir: Path, source: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """A close during the copy throws the new copy away, so no copy outlives the deleted chat."""
    started = threading.Event()
    release = threading.Event()
    real_copy = fw.copy_folder
    errors: list[BaseException] = []

    def slow_copy(*args: Any) -> dict[str, bytes]:
        """Wait for the test before the copy.

        Args:
            args: The :func:`copy_folder` arguments.

        Returns:
            The copy's snapshot.
        """
        started.set()
        assert release.wait(10)
        return real_copy(*args)

    def open_it() -> None:
        """Open the chat's copy, and keep the error it raises."""
        try:
            open_workspace("chat1", source)
        except ValueError as error:
            errors.append(error)

    monkeypatch.setattr(fw, "copy_folder", slow_copy)
    opener = threading.Thread(target=open_it)
    opener.start()
    assert started.wait(5)
    close_workspace("chat1")
    release.set()
    opener.join(10)
    assert [str(error) for error in errors] == ["The chat closed while its folder was copied."]
    assert live_workspace("chat1") is None
    assert not session_folder("chat1").exists()


def test_two_chats_hold_two_copies_at_the_same_time(state_dir: Path, source: Path) -> None:
    """Each chat edits its own copy, and closing one leaves the other."""
    first, _ = open_workspace("chat1", source)
    second, _ = open_workspace("chat2", source)
    assert first.folder != second.folder
    first.write("README.md", "# One\n")
    assert (second.folder / "README.md").read_text() == "# Project\n"
    close_workspace("chat1")
    assert live_workspace("chat2") is second
    assert second.folder.exists()
    assert not first.folder.exists()


def test_deleting_a_chat_deletes_its_folder_copy(state_dir: Path, source: Path) -> None:
    """The chat delete path closes the copy with the chat's other sandbox state."""
    workspace, _ = open_workspace("chat1", source)
    stop_chat_runner("chat1").join(10)
    assert workspace.closed
    assert live_workspace("chat1") is None
    assert not session_folder("chat1").exists()


def test_revert_restores_the_folder_file_or_removes_a_new_one(state_dir: Path, source: Path) -> None:
    """A refused change goes back to the folder's bytes and mode; a file the folder lacks goes away."""
    workspace, _ = open_workspace("chat1", source)
    (workspace.folder / "run.sh").unlink()
    (workspace.folder / "new.txt").write_text("n\n")
    workspace.revert("run.sh")
    workspace.revert("new.txt")
    assert (workspace.folder / "run.sh").read_text() == "echo hi\n"
    assert (workspace.folder / "run.sh").stat().st_mode & 0o111
    assert not (workspace.folder / "new.txt").exists()


def test_a_tracked_path_the_copy_lost_is_dropped(state_dir: Path, source: Path) -> None:
    """A tracked draft file that the folder never had and the copy no longer has is in ``dropped``."""
    workspace, _ = open_workspace("chat1", source)
    workspace.write("notes.txt", "n\n")
    assert workspace.changes(["notes.txt"]).dropped == []
    (workspace.folder / "notes.txt").unlink()
    found = workspace.changes(["notes.txt", "README.md"])
    assert found.dropped == ["notes.txt"]
    assert found.deleted == []


@on_mac
def test_a_command_reads_the_chat_attachments_next_to_the_copy(state_dir: Path, source: Path) -> None:
    """The chat's files sit outside the copy, a command reads them, and a removed file goes away."""
    held = {"m1"}

    def load(skip: set[str]) -> tuple[set[str], list[SimpleNamespace]]:
        """Give the chat's files that the copy does not hold yet.

        Args:
            skip: The ids the copy holds.

        Returns:
            The chat's ids, and the new files.
        """
        files = [SimpleNamespace(id="m1", name="data.csv", data=b"a,b\n1,2\n")]
        return set(held), [file for file in files if file.id in held and file.id not in skip]

    workspace, _ = open_workspace("chat1", source, attachments=load)
    result = workspace.exec_command("cat ../attachments/m1/data.csv")
    assert "1,2" in result
    assert workspace.changes().files == {}
    held.clear()
    workspace.exec_command("true")
    assert not (workspace.session / fw.ATTACHMENTS_DIR / "m1").exists()


def _case_insensitive(folder: Path) -> bool:
    """Say whether a folder's file system ignores the case of names.

    Args:
        folder: A folder on the file system.

    Returns:
        True when ``A`` and ``a`` name one file.
    """
    probe = folder / "CaseProbe"
    probe.write_text("")
    try:
        return (folder / "caseprobe").exists()
    finally:
        probe.unlink()


def test_a_case_only_rename_records_the_old_name_as_deleted(state_dir: Path, source: Path) -> None:
    """On a file system that ignores case, a rename of Utils.py to utils.py still deletes Utils.py."""
    (source / "Utils.py").write_text("u\n")
    workspace, _ = open_workspace("chat1", source)
    if not _case_insensitive(workspace.folder):
        pytest.skip("the file system is case-sensitive")

    (workspace.folder / "Utils.py").rename(workspace.folder / "utils.py")
    found = workspace.changes(["Utils.py"])

    assert found.deleted == ["Utils.py"]
    assert found.files == {"utils.py": "u\n"}


def test_the_copy_streams_each_file_and_refuses_a_folder_past_its_cap(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Small blocks still give the folder's digests, and files past the cap stop the copy with a clear error."""
    source = tmp_path / "project"
    source.mkdir()
    (source / "big.bin").write_bytes(b"a" * 10)
    (source / "run.sh").write_bytes(b"echo hi\n")
    (source / "run.sh").chmod(0o755)
    entries, _ = folder_entries(source, limit=None)
    monkeypatch.setattr(fw, "COPY_CHUNK_BYTES", 3)
    monkeypatch.setattr(fw, "COPY_BYTES_MAX", 18)

    copy = tmp_path / "copy"
    copy.mkdir()
    digests = fw.copy_folder(source, copy, entries)

    assert digests == {rel: file_digest(source, rel) for rel in ("big.bin", "run.sh")}
    assert (copy / "big.bin").read_bytes() == b"a" * 10
    monkeypatch.setattr(fw, "COPY_BYTES_MAX", 17)
    with pytest.raises(FolderTreeError, match="more than the setup shell can copy"):
        fw.copy_folder(source, tmp_path / "unused", entries)
    # A file that grew after the listing is counted while it streams, not only in the listing.
    (source / "big.bin").write_bytes(b"a" * 20)
    grown = tmp_path / "grown"
    grown.mkdir()
    with pytest.raises(FolderTreeError, match="more than the setup shell can copy"):
        fw.copy_folder(source, grown, entries)
