"""Tests for packing a folder on this computer as a repository run's source (SKY-990)."""

from __future__ import annotations

import hashlib
import io
import os
import tarfile
import zipfile
from collections.abc import Callable
from pathlib import Path

import pytest

from core.config import settings
from core.local import folder_tree
from core.worker import local_folder
from core.worker.local_folder import LocalFolderError


@pytest.fixture(autouse=True)
def local_state(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> Path:
    """Run each test as the desktop local stack, with its own state folder.

    Args:
        monkeypatch: Restores the settings after the test.
        tmp_path: Scratch folder.

    Returns:
        The local state folder.
    """
    state = tmp_path / "state"
    monkeypatch.setattr(settings, "local_mode", True)
    monkeypatch.setattr(settings, "local_state_dir", str(state))
    return state


def _write(root: Path, files: dict[str, str]) -> Path:
    """Write text files below a folder.

    Args:
        root: The folder.
        files: Root-relative path to text.

    Returns:
        The folder.
    """
    for name, text in files.items():
        path = root / name
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(text)
    return root


def _state(root: Path) -> dict[str, tuple[float, bytes | None]]:
    """Record each path's mtime and content, to prove a pack writes nothing.

    Args:
        root: The folder.

    Returns:
        Path to mtime and bytes (``None`` for a folder).
    """
    seen: dict[str, tuple[float, bytes | None]] = {".": (root.stat().st_mtime, None)}
    for path in root.rglob("*"):
        seen[str(path.relative_to(root))] = (path.lstat().st_mtime, path.read_bytes() if path.is_file() else None)
    return seen


def test_the_pack_is_deterministic_and_kept_and_never_writes_the_folder(tmp_path: Path, local_state: Path) -> None:
    """The same files give the same commit, the original is kept, and the folder stays as it was."""
    root = _write(tmp_path / "app", {"src/main.py": "print('hi')\n", "README.md": "hi\n"})
    (root / "link.py").symlink_to("src/main.py")
    (root / "linked").symlink_to(root / "src")
    before = _state(root)
    first_dir, second_dir = tmp_path / "w1", tmp_path / "w2"
    first_dir.mkdir()
    second_dir.mkdir()

    first = local_folder.snapshot_folder(root, first_dir)
    os.utime(root / "README.md", (1, 1))
    second = local_folder.snapshot_folder(root, second_dir)

    assert first.commit == second.commit
    assert len(first.commit) == 40
    kept = local_state / local_folder.KEPT_FOLDER / f"{first.commit}.tgz"
    assert kept.read_bytes() == first.archive.read_bytes()
    with tarfile.open(first.archive) as packed:
        # Links are never packed or followed.
        assert packed.getnames() == ["README.md", "src", "src/main.py"]
        assert {member.mtime for member in packed.getmembers()} == {0}
    after = _state(root)
    after["README.md"] = before["README.md"]
    assert after == before


def test_an_empty_folder_packs(tmp_path: Path) -> None:
    """An empty folder is a valid start for a run that writes everything from drafts."""
    root = tmp_path / "empty"
    root.mkdir()
    workdir = tmp_path / "w"
    workdir.mkdir()

    snapshot = local_folder.snapshot_folder(root, workdir)

    with tarfile.open(snapshot.archive) as packed:
        assert packed.getnames() == []
    assert local_folder.snapshot_tree(snapshot.commit) == {"entries": [], "truncated": False}


def test_a_folder_over_the_cap_names_its_largest_folders(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    """The size error says which top-level folders to add to ``.gitignore``, and leaves no archive."""
    monkeypatch.setattr(local_folder, "MAX_ARCHIVE_BYTES", 4096)
    root = tmp_path / "app"
    for name, size in (("data", 300_000), ("assets", 200_000), ("src", 10)):
        (root / name).mkdir(parents=True)
        (root / name / "blob.bin").write_bytes(os.urandom(size))
    workdir = tmp_path / "w"
    workdir.mkdir()

    with pytest.raises(LocalFolderError, match=r"The largest top-level folders are data/ .*, assets/ .*\.gitignore"):
        local_folder.snapshot_folder(root, workdir)

    assert not (workdir / "tree.tgz").exists()


def test_a_missing_folder_is_refused(tmp_path: Path) -> None:
    """A path that is not a folder fails in plain words."""
    with pytest.raises(LocalFolderError, match="does not exist"):
        local_folder.snapshot_folder(tmp_path / "gone", tmp_path)


def test_a_pinned_run_reuses_its_kept_copy_and_refuses_a_changed_folder(tmp_path: Path, local_state: Path) -> None:
    """A resume reads the kept tree; without the copy, a folder that changed is refused."""
    root = _write(tmp_path / "app", {"main.py": "x = 1\n"})
    workdir = tmp_path / "w"
    workdir.mkdir()
    commit = local_folder.snapshot_folder(root, workdir).commit
    (root / "main.py").write_text("x = 2\n")

    resumed = local_folder.snapshot_folder(root, workdir, commit)
    with tarfile.open(resumed.archive) as packed:
        assert packed.extractfile("main.py").read() == b"x = 1\n"  # type: ignore[union-attr]

    local_folder.kept_snapshot(commit).unlink()
    with pytest.raises(LocalFolderError, match="The folder changed after the run was set up"):
        local_folder.snapshot_folder(root, workdir, commit)


@pytest.mark.parametrize(
    ("local_mode", "provider", "allowed"),
    [(True, None, True), (True, "docker", True), (True, "vercel", False), (False, None, False)],
)
def test_a_local_source_runs_only_on_this_computers_docker_box(
    monkeypatch: pytest.MonkeyPatch, local_mode: bool, provider: str | None, allowed: bool
) -> None:
    """Any remote provider, or a hosted deployment, refuses the folder."""
    monkeypatch.setattr(settings, "local_mode", local_mode)

    if allowed:
        local_folder.ensure_runs_here(provider)
    else:
        with pytest.raises(LocalFolderError, match=r"A local folder runs only on this computer\."):
            local_folder.ensure_runs_here(provider)


def test_the_kept_copy_serves_the_tree_files_and_a_patched_zip(tmp_path: Path) -> None:
    """The file browser and the download read the kept tree, with drafts and one version's patch."""
    root = _write(tmp_path / "app", {"src/main.py": "x = 1\n", "README.md": "hi\n"})
    (root / "outside").symlink_to("/etc/hosts")
    workdir = tmp_path / "w"
    workdir.mkdir()
    commit = local_folder.snapshot_folder(root, workdir).commit
    patch = "--- a/src/main.py\n+++ b/src/main.py\n@@ -1 +1 @@\n-x = 1\n+x = 2\n"
    zip_dir = tmp_path / "zip"
    zip_dir.mkdir()

    tree = local_folder.snapshot_tree(commit)
    found = local_folder.snapshot_file(commit, "src/main.py", 1000)
    missing = local_folder.snapshot_file(commit, "src/new.py", 1000)
    large = local_folder.snapshot_file(commit, "src/main.py", 2)
    archive = local_folder.snapshot_zip(
        commit, patch, zip_dir, folder="app", drafts={"cfg/ascent.toml": "[run]\n"}, readonly_paths=()
    )

    assert tree["entries"] == [
        {"path": "README.md", "type": "file", "size": 3},
        {"path": "src", "type": "dir"},
        {"path": "src/main.py", "type": "file", "size": 6},
    ]
    assert found == {"content": b"x = 1\n", "size": 6, "missing": False, "too_large": False}
    assert missing["missing"] is True
    assert large == {"content": None, "size": 6, "missing": False, "too_large": True}
    with zipfile.ZipFile(archive) as zipped:
        assert sorted(zipped.namelist()) == ["app/README.md", "app/cfg/ascent.toml", "app/src/main.py"]
        assert zipped.read("app/src/main.py") == b"x = 2\n"
    assert (root / "src" / "main.py").read_text() == "x = 1\n"


def test_a_run_without_its_kept_copy_says_so(tmp_path: Path) -> None:
    """A missing copy, or a commit that is not a SHA-1, fails in plain words."""
    with pytest.raises(LocalFolderError, match="missing on this computer"):
        local_folder.snapshot_tree("a" * 40)
    with pytest.raises(LocalFolderError, match="not valid"):
        local_folder.kept_snapshot("../../etc")


def test_the_pack_commit_is_the_folder_source_id(tmp_path: Path) -> None:
    """The run's commit equals the setup agent's draft base for the same folder."""
    root = _write(
        tmp_path / "app", {"src/main.py": "x = 1\n", ".env": "KEY=1\n", ".gitignore": "*.log\n", "a.log": "1\n"}
    )
    workdir = tmp_path / "w"
    workdir.mkdir()

    assert local_folder.snapshot_folder(root, workdir).commit == folder_tree.folder_source_id(root)


class _ChangingFile(io.BufferedReader):
    """A file that changes on disk right after the pack's first read of it."""

    def __init__(self, path: Path, change: Callable[[Path], None]) -> None:
        """Open the file.

        Args:
            path: The file.
            change: Called with the path once, after the first read.
        """
        super().__init__(io.FileIO(path, "rb"), buffer_size=1)
        self._path = path
        self._change: Callable[[Path], None] | None = change

    def read(self, size: int | None = -1) -> bytes:
        """Read, then change the file once.

        Args:
            size: The most bytes to read.

        Returns:
            The bytes read.
        """
        data = super().read(size)
        if self._change is not None:
            change, self._change = self._change, None
            change(self._path)
        return data


def _pack_while_changing(tmp_path: Path, monkeypatch: pytest.MonkeyPatch, change: Callable[[Path], None]) -> tuple:
    """Pack a folder whose one big file changes during the pack.

    Args:
        tmp_path: Scratch folder.
        monkeypatch: Pytest monkeypatch fixture.
        change: Changes the file after the pack's first read.

    Returns:
        The snapshot and the packed bytes of the file.
    """
    root = tmp_path / "app"
    root.mkdir()
    (root / "big.bin").write_bytes(b"a" * 65_536)
    monkeypatch.setattr(local_folder, "open_no_follow", lambda path: _ChangingFile(path, change))
    workdir = tmp_path / "w"
    workdir.mkdir()
    snapshot = local_folder.snapshot_folder(root, workdir)
    with tarfile.open(snapshot.archive, "r:gz") as packed:
        handle = packed.extractfile("big.bin")
        assert handle is not None
        return snapshot, handle.read()


def test_a_file_rewritten_during_the_pack_gives_an_id_of_the_packed_bytes(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """The pack reads each file once, so the id matches the packed bytes, not a second read."""

    def rewrite(path: Path) -> None:
        """Rewrite the file in place with the same size."""
        with path.open("r+b") as handle:
            handle.write(b"b" * 65_536)

    snapshot, body = _pack_while_changing(tmp_path, monkeypatch, rewrite)

    assert b"a" in body
    assert b"b" in body
    assert snapshot.commit == folder_tree.source_id([("big.bin", hashlib.sha1(body, usedforsecurity=False).digest())])


def test_a_file_that_grows_during_the_pack_is_cut_at_its_listed_size(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Bytes added during the pack stay out of the pack and the id alike."""

    def grow(path: Path) -> None:
        """Append to the file."""
        with path.open("ab") as handle:
            handle.write(b"c" * 1000)

    snapshot, body = _pack_while_changing(tmp_path, monkeypatch, grow)

    assert body == b"a" * 65_536
    assert snapshot.commit == folder_tree.source_id([("big.bin", hashlib.sha1(body, usedforsecurity=False).digest())])


def test_the_pack_keeps_only_the_execute_bit_and_the_id_follows_it(tmp_path: Path) -> None:
    """Modes become 0o755 or 0o644, so umask and group bits move neither the pack nor the id."""
    root = _write(tmp_path / "app", {"run.sh": "echo hi\n", "lib/a.py": "x = 1\n"})
    (root / "run.sh").chmod(0o700)
    (root / "lib/a.py").chmod(0o600)
    (root / "lib").chmod(0o700)
    first_dir, second_dir = tmp_path / "w1", tmp_path / "w2"
    first_dir.mkdir()
    second_dir.mkdir()

    first = local_folder.snapshot_folder(root, first_dir)
    with tarfile.open(first.archive, "r:gz") as packed:
        modes = {member.name: member.mode for member in packed.getmembers()}
    (root / "run.sh").chmod(0o644)
    second = local_folder.snapshot_folder(root, second_dir)

    assert modes == {"lib": 0o755, "lib/a.py": 0o644, "run.sh": 0o755}
    assert first.commit != second.commit
    assert second.commit == folder_tree.folder_source_id(root)
