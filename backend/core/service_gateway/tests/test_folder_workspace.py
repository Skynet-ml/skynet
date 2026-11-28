"""Tests for the setup agent's private copy of a local folder (SKY-997)."""

from __future__ import annotations

import hashlib
import os
import subprocess
import sys
import time
from collections.abc import Iterator
from pathlib import Path
from types import SimpleNamespace
from typing import Any

import pytest

from core.config import settings
from core.service_gateway.agents import folder_workspace as fw
from core.service_gateway.agents.apply_patch import apply_patch
from core.service_gateway.agents.folder_workspace import (
    FolderWorkspace,
    close_workspace,
    live_workspace,
    open_workspace,
    saved_setup_secrets,
    session_folder,
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
    monkeypatch.setattr(fw, "_swept", [])
    yield state
    for key in list(fw._WORKSPACES):
        close_workspace(key)


@pytest.fixture
def source(tmp_path: Path) -> Path:
    """Make a user folder with files that must and must not reach the copy.

    Args:
        tmp_path: The test's folder.

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


def test_the_first_open_deletes_copies_an_earlier_process_left(state_dir: Path, source: Path) -> None:
    stale = session_folder("old-chat") / "folder"
    stale.mkdir(parents=True)
    open_workspace("chat1", source)
    assert not stale.exists()


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
