"""Tests for packing a folder on this computer as a repository run's source (SKY-990)."""

from __future__ import annotations

import errno
import fcntl
import hashlib
import io
import os
import subprocess
import tarfile
import threading
import time
import unicodedata
import zipfile
from collections.abc import Callable
from pathlib import Path

import pytest

from core.config import settings
from core.local import folder_tree
from core.service_gateway.optimization.blackbox.repo_tree import NOT_CLEAN, apply_patch, case_insensitive
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


def _age(path: Path) -> float:
    """Set a file's mtime to before the fresh-pack window.

    Args:
        path: The file.

    Returns:
        The new mtime.
    """
    old = time.time() - local_folder.FRESH_PACK_SECONDS - 60
    os.utime(path, (old, old))
    return old


def test_forget_snapshot_keeps_a_recent_pack(tmp_path: Path) -> None:
    """A pack written in the last minutes may belong to a run that has not stored its commit yet."""
    root = _write(tmp_path / "app", {"main.py": "x = 1\n"})
    workdir = tmp_path / "w"
    workdir.mkdir()
    commit = local_folder.snapshot_folder(root, workdir).commit

    assert local_folder.forget_snapshot(commit) is False
    assert local_folder.kept_snapshot(commit).is_file()


def test_reusing_a_kept_pack_marks_it_as_recent(tmp_path: Path) -> None:
    """A later pass that reuses the pack refreshes its mtime, so a cleanup keeps it."""
    root = _write(tmp_path / "app", {"main.py": "x = 1\n"})
    first = tmp_path / "w1"
    first.mkdir()
    commit = local_folder.snapshot_folder(root, first).commit
    kept = local_folder.kept_snapshot(commit)
    old = _age(kept)
    second = tmp_path / "w2"
    second.mkdir()

    local_folder.snapshot_folder(root, second, commit)

    assert kept.stat().st_mtime > old + local_folder.FRESH_PACK_SECONDS
    assert local_folder.forget_snapshot(commit) is False


def test_forget_snapshot_deletes_only_the_named_pack(tmp_path: Path, local_state: Path) -> None:
    """The kept pack goes, a second call is a no-op, and a commit with a path in it is refused."""
    root = _write(tmp_path / "app", {"main.py": "x = 1\n"})
    workdir = tmp_path / "w"
    workdir.mkdir()
    commit = local_folder.snapshot_folder(root, workdir).commit
    kept = local_folder.kept_snapshot(commit)
    _age(kept)
    outside = local_state / "outside.tgz"
    outside.write_bytes(b"keep")

    assert local_folder.forget_snapshot(commit) is True
    assert local_folder.forget_snapshot(commit) is False

    assert not kept.exists()
    with pytest.raises(LocalFolderError, match="not valid"):
        local_folder.forget_snapshot("../outside")
    assert outside.is_file()


@pytest.mark.parametrize("changed", [False, True], ids=["same-folder", "changed-folder"])
def test_a_reuse_during_a_cleanup_waits_and_then_packs_the_folder_again(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, changed: bool
) -> None:
    """A run that reuses the pack between the cleanup's check and delete waits for the lock.

    The cleanup then deletes the pack, and the run packs the folder again: the
    same folder gives the same pack back, and a changed folder fails in plain
    words. No run ever holds a copy of a pack that the cleanup deletes after.
    """
    root = _write(tmp_path / "app", {"main.py": "x = 1\n"})
    first = tmp_path / "w1"
    first.mkdir()
    commit = local_folder.snapshot_folder(root, first).commit
    kept = local_folder.kept_snapshot(commit)
    _age(kept)
    if changed:
        (root / "main.py").write_text("x = 2\n")
    second = tmp_path / "w2"
    second.mkdir()
    reused: list[object] = []

    def run() -> None:
        """Reuse the pack, and keep the commit or the error."""
        try:
            reused.append(local_folder.snapshot_folder(root, second, commit).commit)
        except LocalFolderError as error:
            reused.append(error)

    reuse = threading.Thread(target=run)
    cleanup = threading.get_ident()
    real_kept, real_keep = local_folder.kept_snapshot, local_folder._keep
    kept_by_reuse: list[str] = []
    seen: dict[str, bool] = {}

    class _Paused(type(kept)):  # type: ignore[misc]
        """A pack path whose stat starts the reuse, before the cleanup's unlink."""

        def stat(self, *, follow_symlinks: bool = True) -> os.stat_result:
            """Stat the pack, then check the lock and start the reuse.

            Args:
                follow_symlinks: Passed on to ``Path.stat``.

            Returns:
                The pack's status from before the reuse.
            """
            status = super().stat(follow_symlinks=follow_symlinks)
            # Only the pack's own stat; its folder's stat comes before the lock.
            if self == kept and reuse.ident is None:
                with (kept.parent / ".lock").open("a") as probe:
                    try:
                        fcntl.flock(probe, fcntl.LOCK_EX | fcntl.LOCK_NB)
                        seen["locked"] = False
                    except BlockingIOError:
                        seen["locked"] = True
                reuse.start()
                # The reuse cannot finish while the cleanup holds the lock, however long this waits.
                reuse.join(timeout=0.2)
                seen["waiting"] = reuse.is_alive()
            return status

    def paused(name: str) -> Path:
        """Give the cleanup the pausing path, and the reuse the plain one.

        Args:
            name: The commit.

        Returns:
            The pack's path.
        """
        path = real_kept(name)
        return _Paused(path) if threading.get_ident() == cleanup else path

    def keep(archive: Path, digest: str) -> None:
        """Count the packs the reuse writes, and write them.

        Args:
            archive: The packed tree.
            digest: Its SHA-1.
        """
        kept_by_reuse.append(digest)
        real_keep(archive, digest)

    monkeypatch.setattr(local_folder, "kept_snapshot", paused)
    monkeypatch.setattr(local_folder, "_keep", keep)

    assert local_folder.forget_snapshot(commit) is True
    reuse.join(timeout=10)

    assert seen == {"locked": True, "waiting": True}
    if changed:
        assert [str(error) for error in reused] == [
            "The folder changed after the run was set up. Set up the run again."
        ]
        assert kept_by_reuse == []
        assert not kept.exists()
    else:
        assert reused == [commit]
        assert kept_by_reuse == [commit]
        assert kept.is_file()
        assert (second / "tree.tgz").is_file()


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
    monkeypatch.setattr(local_folder, "open_no_follow", lambda folder, rel: _ChangingFile(folder / rel, change))
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


def _pack_after_swap(tmp_path: Path, monkeypatch: pytest.MonkeyPatch, swap: Callable[[Path], None]) -> Path:
    """Change a folder between its listing and its pack.

    Args:
        tmp_path: Scratch folder.
        monkeypatch: Pytest monkeypatch fixture.
        swap: Changes the folder right after the listing.

    Returns:
        The work folder that the pack writes into.
    """
    real = local_folder.folder_entries

    def list_then_swap(root: Path, limit: int | None = None) -> tuple:
        """List the folder, then change it before the pack reads it."""
        listed = real(root, limit=limit)
        swap(root)
        return listed

    monkeypatch.setattr(local_folder, "folder_entries", list_then_swap)
    workdir = tmp_path / "w"
    workdir.mkdir()
    return workdir


def test_a_subfolder_swapped_for_a_link_during_the_pack_is_refused(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """A listed subfolder that becomes a link to an outside folder never leads the pack out."""
    root = _write(tmp_path / "app", {"sub/hosts": "inside\n"})
    outside = _write(tmp_path / "secret", {"hosts": "outside\n"})

    def swap(folder: Path) -> None:
        """Turn the listed subfolder into a link out of the folder."""
        (folder / "sub" / "hosts").unlink()
        (folder / "sub").rmdir()
        (folder / "sub").symlink_to(outside)

    workdir = _pack_after_swap(tmp_path, monkeypatch, swap)

    with pytest.raises(LocalFolderError, match="could not be read"):
        local_folder.snapshot_folder(root, workdir)
    assert not (workdir / "tree.tgz").exists()


def test_a_file_swapped_for_a_pipe_during_the_pack_is_refused_without_blocking(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """A listed file that becomes a pipe with no writer is refused at once, not waited on."""
    root = _write(tmp_path / "app", {"a.txt": "x"})

    def swap(folder: Path) -> None:
        """Turn the listed file into a pipe."""
        (folder / "a.txt").unlink()
        os.mkfifo(folder / "a.txt")

    workdir = _pack_after_swap(tmp_path, monkeypatch, swap)
    errors: list[BaseException] = []

    def pack() -> None:
        """Pack the folder and record its error."""
        try:
            local_folder.snapshot_folder(root, workdir)
        except BaseException as error:
            errors.append(error)

    worker = threading.Thread(target=pack, daemon=True)
    worker.start()
    worker.join(10)

    assert not worker.is_alive()
    assert len(errors) == 1
    assert isinstance(errors[0], LocalFolderError)
    assert "Not a regular file" in str(errors[0])


def _fingerprint(root: Path) -> str:
    """Hash every path below a folder: its kind, permission bits, bytes and link target.

    Args:
        root: The folder.

    Returns:
        One SHA-256 over the whole folder, ignored files included.
    """
    digest = hashlib.sha256()
    for path in sorted(root.rglob("*")):
        status = path.lstat()
        digest.update(f"{path.relative_to(root)}|{status.st_mode:o}|".encode())
        if path.is_symlink():
            digest.update(str(path.readlink()).encode())
        elif path.is_file():
            digest.update(path.read_bytes())
    return digest.hexdigest()


_PATCH = (
    "--- a/src/main.py\n+++ b/src/main.py\n@@ -1 +1 @@\n-x = 1\n+x = 2\n"
    "--- /dev/null\n+++ b/src/deep/new.py\n@@ -0,0 +1 @@\n+y = 1\n"
    "--- a/README.md\n+++ /dev/null\n@@ -1 +0,0 @@\n-hi\n"
)


def _applied_run(tmp_path: Path) -> tuple[Path, str]:
    """Pack a folder with ignored files, a link and an executable script.

    Args:
        tmp_path: Scratch folder.

    Returns:
        The folder and its commit.
    """
    root = _write(
        tmp_path / "app",
        {"src/main.py": "x = 1\n", "README.md": "hi\n", ".env": "KEY=1\n", "node_modules/a.js": "1\n", "run.sh": "a\n"},
    )
    (root / "run.sh").chmod(0o700)
    (root / "outside").symlink_to("/etc/hosts")
    workdir = tmp_path / "w"
    workdir.mkdir()
    return root, local_folder.snapshot_folder(root, workdir).commit


def _apply(root: Path, commit: str, patch: str, run_id: str, **rules: object) -> list[str]:
    """Apply a version with every path editable and none blocked, unless a test names the rules.

    Args:
        root: The run's folder.
        commit: The run's pinned source id.
        patch: The version's patch.
        run_id: The run.
        **rules: ``apply_version`` keywords, such as ``drafts`` or ``blocked_paths``.

    Returns:
        The changed files.
    """
    rules.setdefault("editable_paths", ["."])
    rules.setdefault("blocked_paths", [])
    return local_folder.apply_version(root, commit, patch, run_id, **rules)  # type: ignore[arg-type]


def test_apply_writes_the_version_and_undo_restores_every_byte_and_mode(tmp_path: Path, local_state: Path) -> None:
    """Apply writes the drafts and the patch; Undo gives back the same folder hash, created folders removed."""
    root, commit = _applied_run(tmp_path)
    before = _fingerprint(root)

    changed = _apply(root, commit, _PATCH, "run-1", drafts={"cfg/a.toml": "[run]\n"})

    assert changed == ["README.md", "cfg/a.toml", "src/deep/new.py", "src/main.py"]
    assert (root / "src/main.py").read_text() == "x = 2\n"
    assert (root / "src/deep/new.py").read_text() == "y = 1\n"
    assert (root / "cfg/a.toml").read_text() == "[run]\n"
    assert not (root / "README.md").exists()
    assert (root / "run.sh").stat().st_mode & 0o777 == 0o700
    assert (root / ".env").read_text() == "KEY=1\n"
    assert local_folder.is_applied("run-1")
    local_folder.check_applied(root, "run-1")
    assert not list(root.rglob("*.apply"))
    with pytest.raises(LocalFolderError, match="already in the folder"):
        _apply(root, commit, _PATCH, "run-1")

    restored = local_folder.undo_version(root, "run-1")

    assert restored == changed
    assert _fingerprint(root) == before
    assert not (root / "cfg").exists()
    assert not (root / "src/deep").exists()
    # The record stays until the caller saves the run, so a repeat Undo finds the files restored.
    assert local_folder.undo_version(root, "run-1") == changed
    assert _fingerprint(root) == before
    local_folder.forget_apply("run-1")
    assert not local_folder.is_applied("run-1")
    with pytest.raises(LocalFolderError, match="nothing to undo"):
        local_folder.undo_version(root, "run-1")


def test_apply_writes_nothing_when_a_file_to_change_changed(tmp_path: Path) -> None:
    """A file the version changes, adds or deletes that no longer matches the kept tree stops Apply before any write."""
    root, commit = _applied_run(tmp_path)
    (root / "src/main.py").write_text("x = 9\n")
    (root / "src/deep").mkdir()
    (root / "src/deep/new.py").write_text("mine\n")
    before = _fingerprint(root)

    with pytest.raises(local_folder.LocalFolderConflictError) as caught:
        _apply(root, commit, _PATCH, "run-1")

    assert caught.value.paths == ["src/deep/new.py", "src/main.py"]
    assert _fingerprint(root) == before
    assert not local_folder.is_applied("run-1")


def test_apply_refuses_a_link_on_the_way_and_writes_nothing(tmp_path: Path) -> None:
    """A folder swapped for a link to somewhere else is a conflict, so no write leaves the folder."""
    root, commit = _applied_run(tmp_path)
    elsewhere = _write(tmp_path / "elsewhere", {"main.py": "x = 1\n"})
    (root / "src/main.py").unlink()
    (root / "src").rmdir()
    (root / "src").symlink_to(elsewhere)
    before, outside = _fingerprint(root), _fingerprint(elsewhere)

    with pytest.raises(local_folder.LocalFolderConflictError) as caught:
        _apply(root, commit, _PATCH, "run-1")

    assert caught.value.paths == ["src/deep/new.py", "src/main.py"]
    assert _fingerprint(root) == before
    assert _fingerprint(elsewhere) == outside


def test_apply_refuses_a_file_swapped_for_a_link(tmp_path: Path) -> None:
    """A file to change that is now a link is a conflict, and the link target is not touched."""
    root, commit = _applied_run(tmp_path)
    target = _write(tmp_path / "t", {"main.py": "x = 1\n"}) / "main.py"
    (root / "src/main.py").unlink()
    (root / "src/main.py").symlink_to(target)

    with pytest.raises(local_folder.LocalFolderConflictError):
        _apply(root, commit, _PATCH, "run-1")

    assert target.read_text() == "x = 1\n"
    assert (root / "src/main.py").is_symlink()


def test_apply_refuses_a_version_that_changes_a_path_the_copy_leaves_out(tmp_path: Path) -> None:
    """A draft at a path the folder copy skips (``node_modules``) is refused, since the kept tree cannot vouch for it."""
    root, commit = _applied_run(tmp_path)
    before = _fingerprint(root)

    with pytest.raises(LocalFolderError, match="leaves out"):
        _apply(root, commit, "", "run-1", drafts={"node_modules/b.js": "2\n"})

    assert _fingerprint(root) == before


def test_apply_puts_back_written_files_when_a_later_write_fails(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """A write that fails half way rolls back the files already written and keeps no Undo record."""
    root, commit = _applied_run(tmp_path)
    before = _fingerprint(root)
    real_replace = os.replace
    calls = {"n": 0}

    def flaky_replace(*args: object, **kwargs: object) -> None:
        """Fail the third rename of an Apply.

        Args:
            *args: Passed to ``os.replace``.
            **kwargs: Passed to ``os.replace``.

        Raises:
            OSError: On the third call.
        """
        calls["n"] += 1
        if calls["n"] == 3:
            raise OSError(28, "No space left on device")
        real_replace(*args, **kwargs)

    monkeypatch.setattr(local_folder.os, "replace", flaky_replace)

    with pytest.raises(LocalFolderError, match="No space left"):
        _apply(root, commit, _PATCH, "run-1", drafts={"cfg/a.toml": "[run]\n"})

    monkeypatch.setattr(local_folder.os, "replace", real_replace)
    assert _fingerprint(root) == before
    assert not local_folder.is_applied("run-1")


def test_undo_writes_nothing_when_a_file_changed_after_apply(tmp_path: Path) -> None:
    """A file edited after Apply stops Undo before any write, and the Undo record stays."""
    root, commit = _applied_run(tmp_path)
    _apply(root, commit, _PATCH, "run-1")
    (root / "src/main.py").write_text("edited\n")
    before = _fingerprint(root)

    with pytest.raises(local_folder.LocalFolderConflictError) as caught:
        local_folder.undo_version(root, "run-1")

    assert caught.value.paths == ["src/main.py"]
    assert _fingerprint(root) == before
    assert local_folder.is_applied("run-1")


def test_a_run_id_that_is_not_a_plain_name_is_refused() -> None:
    """The Undo record's name cannot climb out of the state folder."""
    with pytest.raises(LocalFolderError, match="not valid"):
        local_folder.applied_record("../x")


def _metric_run(tmp_path: Path, name: str) -> tuple[Path, str]:
    """Pack a folder whose metric lives at ``src/<name>/metric.py``.

    Args:
        tmp_path: Scratch folder.
        name: The metric folder's name.

    Returns:
        The folder and its commit.
    """
    root = _write(tmp_path / "app", {f"src/{name}/metric.py": "score = 1\n", "src/main.py": "x = 1\n"})
    workdir = tmp_path / "w"
    workdir.mkdir()
    return root, local_folder.snapshot_folder(root, workdir).commit


def _metric_patch(path: str) -> str:
    """Return a patch that raises the score in one metric file.

    Args:
        path: The file the patch names.

    Returns:
        A git-style patch.
    """
    return f"diff --git a/{path} b/{path}\n--- a/{path}\n+++ b/{path}\n@@ -1 +1 @@\n-score = 1\n+score = 999\n"


@pytest.mark.parametrize(
    ("name", "named", "blocked"),
    [
        ("eval", "src/EVAL/metric.py", "src/eval"),
        ("eval", "src/eval/metric.py", "SRC/Eval"),
        (unicodedata.normalize("NFC", "café"), f"src/{unicodedata.normalize('NFD', 'café')}/metric.py", "src/café"),
    ],
)
def test_apply_refuses_a_case_or_unicode_variant_of_a_blocked_path(
    tmp_path: Path, name: str, named: str, blocked: str
) -> None:
    """A blocked path in another case or Unicode form is still blocked, so the metric file stays as it is.

    On a case-sensitive disk, as on Linux CI, a variant name is another file,
    so the patch does not apply there; on macOS the rule check refuses it.
    """
    root, commit = _metric_run(tmp_path, name)
    before = _fingerprint(root)
    real_name = named.startswith(f"src/{name}/") or case_insensitive(tmp_path)

    with pytest.raises(LocalFolderError, match="may not change" if real_name else None):
        _apply(root, commit, _metric_patch(named), "run-1", editable_paths=["src"], blocked_paths=[blocked])

    assert _fingerprint(root) == before
    assert not local_folder.is_applied("run-1")


def test_apply_refuses_a_new_file_in_a_case_variant_of_a_blocked_folder(tmp_path: Path) -> None:
    """A new file under a blocked folder in another case is refused on every disk, case-sensitive or not."""
    root, commit = _metric_run(tmp_path, "eval")
    before = _fingerprint(root)
    patch = (
        "diff --git a/src/EVAL/new.py b/src/EVAL/new.py\nnew file mode 100644\n"
        "--- /dev/null\n+++ b/src/EVAL/new.py\n@@ -0,0 +1 @@\n+score = 999\n"
    )

    with pytest.raises(LocalFolderError, match="may not change"):
        _apply(root, commit, patch, "run-1", editable_paths=["src"], blocked_paths=["src/eval"])

    assert _fingerprint(root) == before
    assert not local_folder.is_applied("run-1")


def test_undo_refuses_a_record_for_another_folder(tmp_path: Path) -> None:
    """The Undo record names the folder Apply wrote, so Undo never writes its bytes into another folder."""
    root, commit = _applied_run(tmp_path)
    _apply(root, commit, _PATCH, "run-1")
    other = _write(tmp_path / "other", {"src/main.py": "x = 2\n"})
    before = _fingerprint(other)

    with pytest.raises(LocalFolderError, match="another folder"):
        local_folder.undo_version(other, "run-1")

    assert _fingerprint(other) == before
    assert local_folder.is_applied("run-1")


def test_apply_refuses_a_change_outside_the_editable_paths(tmp_path: Path) -> None:
    """The editable paths are checked on the files the version really changes."""
    root, commit = _metric_run(tmp_path, "eval")
    before = _fingerprint(root)
    patch = (
        "diff --git a/docs/a.md b/docs/a.md\nnew file mode 100644\n--- /dev/null\n+++ b/docs/a.md\n@@ -0,0 +1 @@\n+x\n"
    )

    with pytest.raises(LocalFolderError, match="outside the editable paths"):
        _apply(root, commit, patch, "run-1", editable_paths=["src"])

    assert _fingerprint(root) == before


def test_apply_writes_the_setup_drafts_outside_the_rules_but_not_a_patch_over_them(tmp_path: Path) -> None:
    """The run's setup drafts go in as the run used them; a patch that changes a draft file is still checked."""
    root, commit = _metric_run(tmp_path, "eval")
    drafts = {"ascent.toml": "version = 1\n"}
    rules = {"drafts": drafts, "editable_paths": ["src"], "blocked_paths": ["ascent.toml"]}
    over = "diff --git a/ascent.toml b/ascent.toml\n--- a/ascent.toml\n+++ b/ascent.toml\n@@ -1 +1 @@\n-version = 1\n+version = 2\n"
    before = _fingerprint(root)

    with pytest.raises(LocalFolderError, match="may not change"):
        _apply(root, commit, over, "run-1", **rules)
    assert _fingerprint(root) == before

    assert _apply(root, commit, "", "run-1", **rules) == ["ascent.toml"]
    assert (root / "ascent.toml").read_text() == "version = 1\n"


def test_a_failed_rollback_keeps_the_undo_record_so_undo_can_finish(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """When the disk fills during Apply and its rollback, the record stays, and Undo restores the folder later."""
    root, commit = _applied_run(tmp_path)
    before = _fingerprint(root)
    real_put = local_folder._put
    calls = {"n": 0}

    def flaky_put(*args: object, **kwargs: object) -> None:
        """Fail every write after the first, as a full disk does.

        Args:
            *args: Passed to ``_put``.
            **kwargs: Passed to ``_put``.

        Raises:
            OSError: From the second call on.
        """
        calls["n"] += 1
        if calls["n"] >= 2:
            raise OSError(28, "No space left on device")
        real_put(*args, **kwargs)

    monkeypatch.setattr(local_folder, "_put", flaky_put)
    with pytest.raises(LocalFolderError, match="No space left"):
        _apply(root, commit, _PATCH, "run-1")
    assert _fingerprint(root) != before
    assert local_folder.is_applied("run-1")
    with pytest.raises(LocalFolderError, match="Undo first"):
        local_folder.check_applied(root, "run-1")

    monkeypatch.setattr(local_folder, "_put", real_put)
    local_folder.undo_version(root, "run-1")
    local_folder.forget_apply("run-1")

    assert _fingerprint(root) == before
    assert not local_folder.is_applied("run-1")


def test_a_failed_undo_record_write_leaves_no_temporary_file(
    tmp_path: Path, local_state: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """A record write that fails part way removes its temporary file, and Apply writes nothing.

    Args:
        tmp_path: Scratch folder.
        local_state: The local state folder.
        monkeypatch: Makes the record write fail.
    """
    root, commit = _applied_run(tmp_path)
    before = _fingerprint(root)

    def broken_dump(*_args: object, **_kwargs: object) -> None:
        """Fail as a full disk would.

        Raises:
            ValueError: Always.
        """
        raise ValueError("disk full")

    monkeypatch.setattr(local_folder.json, "dump", broken_dump)
    with pytest.raises(ValueError, match="disk full"):
        _apply(root, commit, _PATCH, "run-1")

    assert list((local_state / local_folder.APPLIED_FOLDER).iterdir()) == []
    assert _fingerprint(root) == before
    assert not local_folder.is_applied("run-1")


# The test patches ``os.fdopen`` with the class below, which opens the real one.
_REAL_FDOPEN = os.fdopen


class _FullDiskOnClose:
    """A file whose close fails as a full disk fails the last buffer flush."""

    def __init__(self, *args: object, **kwargs: object) -> None:
        """Open the real file.

        Args:
            *args: ``os.fdopen`` arguments.
            **kwargs: ``os.fdopen`` keywords.
        """
        self._file = _REAL_FDOPEN(*args, **kwargs)  # type: ignore[arg-type]

    def write(self, text: str) -> int:
        """Write to the real file.

        Args:
            text: The text.

        Returns:
            The characters written.
        """
        return self._file.write(text)

    def __enter__(self) -> _FullDiskOnClose:
        """Use the file in a ``with`` block.

        Returns:
            This file.
        """
        return self

    def __exit__(self, *_exc: object) -> None:
        """Close the real file, then fail as the flush would.

        Raises:
            OSError: Always, with ``ENOSPC``.
        """
        self._file.close()
        raise OSError(errno.ENOSPC, "No space left on device")


def _fdopen_full_on_record_close(descriptor: int, mode: str = "r", *args: object, **kwargs: object) -> object:
    """Open a file, and make only the Undo record's text file fail at close.

    Apply reads and writes the folder's files in binary mode, so only the record opens as ``"w"``.

    Args:
        descriptor: The open file descriptor.
        mode: The open mode.
        *args: More ``os.fdopen`` arguments.
        **kwargs: ``os.fdopen`` keywords.

    Returns:
        The real file, or a ``_FullDiskOnClose`` for the record.
    """
    if mode == "w":
        return _FullDiskOnClose(descriptor, mode, *args, **kwargs)
    return _REAL_FDOPEN(descriptor, mode, *args, **kwargs)  # type: ignore[arg-type]


@pytest.mark.parametrize("failure", ["close", "rename"])
def test_a_record_write_that_fails_at_close_or_rename_leaves_no_temporary_file(
    tmp_path: Path, local_state: Path, monkeypatch: pytest.MonkeyPatch, failure: str
) -> None:
    """A full disk at the flush on close or at the rename removes the temporary file, and Apply writes nothing.

    Args:
        tmp_path: Scratch folder.
        local_state: The local state folder.
        monkeypatch: Makes the close or the rename fail.
        failure: Which step fails.
    """
    root, commit = _applied_run(tmp_path)
    before = _fingerprint(root)
    if failure == "close":
        monkeypatch.setattr(local_folder.os, "fdopen", _fdopen_full_on_record_close)
    else:

        def full_disk_replace(self: Path, target: Path) -> Path:
            """Fail as a full disk would.

            Raises:
                OSError: Always, with ``ENOSPC``.
            """
            raise OSError(errno.ENOSPC, "No space left on device")

        monkeypatch.setattr(Path, "replace", full_disk_replace)

    with pytest.raises(LocalFolderError, match="No space left"):
        _apply(root, commit, _PATCH, "run-1")

    assert list((local_state / local_folder.APPLIED_FOLDER).iterdir()) == []
    assert _fingerprint(root) == before


_FOLDER_TO_FILE_PATCH = (
    "diff --git a/pkg/mod.py b/pkg/mod.py\ndeleted file mode 100644\n"
    "--- a/pkg/mod.py\n+++ /dev/null\n@@ -1 +0,0 @@\n-x = 1\n"
    "diff --git a/pkg b/pkg\nnew file mode 100644\n"
    "--- /dev/null\n+++ b/pkg\n@@ -0,0 +1 @@\n+y = 1\n"
)


def test_apply_refuses_a_version_that_turns_a_folder_into_a_file(tmp_path: Path, local_state: Path) -> None:
    """A folder that becomes a file at the same path is refused with its own reason, not a false conflict.

    Args:
        tmp_path: Scratch folder.
        local_state: The local state folder.
    """
    root = _write(tmp_path / "app", {"pkg/mod.py": "x = 1\n", "README.md": "hi\n"})
    workdir = tmp_path / "w"
    workdir.mkdir()
    commit = local_folder.snapshot_folder(root, workdir).commit
    before = _fingerprint(root)

    with pytest.raises(LocalFolderError, match="turns pkg") as caught:
        _apply(root, commit, _FOLDER_TO_FILE_PATCH, "run-1")

    assert not isinstance(caught.value, local_folder.LocalFolderConflictError)
    assert _fingerprint(root) == before
    assert not local_folder.is_applied("run-1")
    assert not (local_state / local_folder.APPLIED_FOLDER).exists() or not any(
        (local_state / local_folder.APPLIED_FOLDER).iterdir()
    )


_FILE_TO_FOLDER_OTHER_CASE_PATCH = (
    "diff --git a/Pkg b/Pkg\n"
    "deleted file mode 100644\n"
    "--- a/Pkg\n"
    "+++ /dev/null\n"
    "@@ -1 +0,0 @@\n"
    "-x = 1\n"
    "diff --git a/pkg/mod.py b/pkg/mod.py\n"
    "new file mode 100644\n"
    "--- /dev/null\n"
    "+++ b/pkg/mod.py\n"
    "@@ -0,0 +1 @@\n"
    "+y = 1\n"
)


def test_apply_refuses_a_version_that_turns_a_file_into_a_folder_in_another_case(
    tmp_path: Path, local_state: Path
) -> None:
    """A file Pkg that becomes a folder pkg/ is the same path on a folded disk, so Apply refuses it.

    Args:
        tmp_path: Scratch folder.
        local_state: The local state folder.
    """
    root = _write(tmp_path / "app", {"Pkg": "x = 1\n", "README.md": "hi\n"})
    workdir = tmp_path / "w"
    workdir.mkdir()
    commit = local_folder.snapshot_folder(root, workdir).commit
    before = _fingerprint(root)

    with pytest.raises(LocalFolderError, match="turns") as caught:
        _apply(root, commit, _FILE_TO_FOLDER_OTHER_CASE_PATCH, "run-1")

    assert not isinstance(caught.value, local_folder.LocalFolderConflictError)
    assert _fingerprint(root) == before
    assert not local_folder.is_applied("run-1")
    assert not (local_state / local_folder.APPLIED_FOLDER).exists() or not any(
        (local_state / local_folder.APPLIED_FOLDER).iterdir()
    )


class _FoldedOs:
    """Stand in for ``os`` in ``local_folder``, finding a name under another case as macOS does.

    It lets the case tests run on a case-sensitive disk, as on Linux CI. Like
    APFS, a rename onto another form of an existing name keeps the old form.
    """

    def __getattr__(self, name: str) -> object:
        """Use the real ``os`` for everything else.

        Args:
            name: The attribute.

        Returns:
            The real attribute.
        """
        return getattr(os, name)

    @staticmethod
    def _form(name: str, dir_fd: int | None) -> str:
        """Find the form under which a folded disk would open a name.

        Args:
            name: The name.
            dir_fd: The open folder it is in, or ``None`` for a full path.

        Returns:
            The listed form of the name, or the name when none is listed.
        """
        if dir_fd is None or "/" in name:
            return name
        listed = os.listdir(dir_fd)  # noqa: PTH208
        if name in listed:
            return name
        return next((entry for entry in listed if entry.casefold() == name.casefold()), name)

    def open(self, path: str, flags: int, mode: int = 0o777, *, dir_fd: int | None = None) -> int:
        """Open a name under any case form.

        Args:
            path: The name.
            flags: The open flags.
            mode: The new file's bits.
            dir_fd: The open folder.

        Returns:
            The open descriptor.

        Raises:
            FileExistsError: For an exclusive create when another form exists.
        """
        form = self._form(path, dir_fd)
        if form != path and flags & os.O_CREAT and flags & os.O_EXCL:
            raise FileExistsError(errno.EEXIST, "File exists", path)
        return os.open(form, flags, mode, dir_fd=dir_fd)

    def mkdir(self, path: str, mode: int = 0o777, *, dir_fd: int | None = None) -> None:
        """Make a folder, refusing one when another form exists.

        Args:
            path: The name.
            mode: The folder's bits.
            dir_fd: The open folder.

        Raises:
            FileExistsError: When another form exists.
        """
        if self._form(path, dir_fd) != path:
            raise FileExistsError(errno.EEXIST, "File exists", path)
        os.mkdir(path, mode, dir_fd=dir_fd)

    def unlink(self, path: str, *, dir_fd: int | None = None) -> None:
        """Delete a file under any case form.

        Args:
            path: The name.
            dir_fd: The open folder.
        """
        os.unlink(self._form(path, dir_fd), dir_fd=dir_fd)

    def rmdir(self, path: str, *, dir_fd: int | None = None) -> None:
        """Delete an empty folder under any case form.

        Args:
            path: The name.
            dir_fd: The open folder.
        """
        os.rmdir(self._form(path, dir_fd), dir_fd=dir_fd)

    def replace(self, src: str, dst: str, *, src_dir_fd: int | None = None, dst_dir_fd: int | None = None) -> None:
        """Rename onto a name, keeping the form of an existing one.

        Args:
            src: The name to move.
            dst: The name to replace.
            src_dir_fd: The source's open folder.
            dst_dir_fd: The target's open folder.
        """
        os.replace(src, self._form(dst, dst_dir_fd), src_dir_fd=src_dir_fd, dst_dir_fd=dst_dir_fd)


@pytest.fixture
def folded_disk(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    """Make the user's folder and the version build act as on a disk that ignores case.

    On such a disk the test uses it as it is. Else ``local_folder`` gets a
    folding ``os``, and git apply refuses every patch, as it refuses a
    case-only rename on macOS, so the build takes its index path.

    Args:
        tmp_path: Scratch folder.
        monkeypatch: Restores ``local_folder`` after the test.
    """
    if case_insensitive(tmp_path):
        return

    def refuse(checkout: Path, patch: str) -> None:
        """Refuse a patch as git apply does for a case-only rename on macOS.

        Args:
            checkout: The unpacked tree.
            patch: The version's patch.

        Raises:
            RuntimeError: For every patch that is not empty.
        """
        if patch.strip():
            raise RuntimeError(f"{NOT_CLEAN}: error: already exists in working directory")

    monkeypatch.setattr(local_folder, "os", _FoldedOs())
    monkeypatch.setattr(local_folder, "apply_patch", refuse)
    monkeypatch.setattr(local_folder, "case_insensitive", lambda directory: True)


def _pinned(tmp_path: Path, files: dict[str, str]) -> tuple[Path, str]:
    """Write a folder and pack it as a run's source.

    Args:
        tmp_path: Scratch folder.
        files: Root-relative path to text.

    Returns:
        The folder and its commit.
    """
    root = _write(tmp_path / "app", files)
    workdir = tmp_path / "w"
    workdir.mkdir()
    return root, local_folder.snapshot_folder(root, workdir).commit


def _listed(root: Path) -> list[str]:
    """List every path below a folder by the names the disk stores.

    Args:
        root: The folder.

    Returns:
        The sorted ``/``-joined paths, folders included.
    """
    return sorted(path.relative_to(root).as_posix() for path in root.rglob("*"))


def test_apply_and_undo_a_setup_that_renames_a_file_by_case(tmp_path: Path, folded_disk: None) -> None:
    """A draft foo.py with Foo.py in ``draft_deleted`` renames the file by case, and Undo renames it back."""
    root, commit = _pinned(tmp_path, {"Foo.py": "old\n", "keep.txt": "k\n"})
    before = _fingerprint(root)
    assert _apply(root, commit, "", "case", drafts={"foo.py": "new\n"}, deleted=["Foo.py"]) == ["Foo.py", "foo.py"]
    assert _listed(root) == ["foo.py", "keep.txt"]
    assert (root / "foo.py").read_text() == "new\n"
    local_folder.check_applied(root, "case")
    local_folder.undo_version(root, "case")
    assert _listed(root) == ["Foo.py", "keep.txt"]
    assert _fingerprint(root) == before


_CASE_RENAME_PATCHES = {
    "rename": "diff --git a/a.txt b/A.txt\nsimilarity index 100%\nrename from a.txt\nrename to A.txt\n",
    "delete-and-add": (
        "diff --git a/a.txt b/a.txt\ndeleted file mode 100644\n--- a/a.txt\n+++ /dev/null\n@@ -1 +0,0 @@\n-a\n"
        "diff --git a/A.txt b/A.txt\nnew file mode 100644\n--- /dev/null\n+++ b/A.txt\n@@ -0,0 +1 @@\n+a\n"
    ),
}


@pytest.mark.parametrize("patch", _CASE_RENAME_PATCHES.values(), ids=_CASE_RENAME_PATCHES.keys())
def test_apply_and_undo_a_patch_that_renames_a_file_by_case(tmp_path: Path, folded_disk: None, patch: str) -> None:
    """A patch that renames a.txt to A.txt builds, applies and undoes, on a disk that ignores case too."""
    root, commit = _pinned(tmp_path, {"a.txt": "a\n", "keep.txt": "k\n"})
    before = _fingerprint(root)
    assert _apply(root, commit, patch, "case") == ["A.txt", "a.txt"]
    assert _listed(root) == ["A.txt", "keep.txt"]
    assert (root / "A.txt").read_text() == "a\n"
    local_folder.check_applied(root, "case")
    local_folder.undo_version(root, "case")
    assert _listed(root) == ["a.txt", "keep.txt"]
    assert _fingerprint(root) == before


_FOLDER_CASE_PATCH = (
    "diff --git a/dir/x b/dir/x\ndeleted file mode 100644\n--- a/dir/x\n+++ /dev/null\n@@ -1 +0,0 @@\n-x\n"
    "diff --git a/Dir/x b/Dir/x\nnew file mode 100644\n--- /dev/null\n+++ b/Dir/x\n@@ -0,0 +1 @@\n+y\n"
)


@pytest.mark.parametrize("by", ["patch", "draft"])
def test_apply_and_undo_a_version_that_renames_a_folder_by_case(tmp_path: Path, folded_disk: None, by: str) -> None:
    """A version that deletes dir/x and adds Dir/x renames the folder, and Undo gives back dir/ and its bits."""
    root, commit = _pinned(tmp_path, {"dir/x": "x\n", "keep.txt": "k\n"})
    (root / "dir").chmod(0o750)
    before = _fingerprint(root)
    if by == "patch":
        changed = _apply(root, commit, _FOLDER_CASE_PATCH, "case")
    else:
        changed = _apply(root, commit, "", "case", drafts={"Dir/x": "y\n"}, deleted=["dir/x"])
    assert changed == ["Dir/x", "dir/x"]
    assert _listed(root) == ["Dir", "Dir/x", "keep.txt"]
    assert (root / "Dir/x").read_text() == "y\n"
    local_folder.check_applied(root, "case")
    assert local_folder.undo_version(root, "case") == ["dir/x", "Dir/x"]
    assert _listed(root) == ["dir", "dir/x", "keep.txt"]
    assert _fingerprint(root) == before
    local_folder.forget_undone(root, "case")
    assert not local_folder.is_applied("case")


@pytest.mark.parametrize("new", ["Dir/a_new", "Dir/z_new"])
def test_apply_and_undo_a_folder_case_rename_that_also_adds_a_file_in_the_folder(
    tmp_path: Path, folded_disk: None, new: str
) -> None:
    """A new file in the renamed folder ends up in Dir/, whether it is written before or after Dir/x."""
    root, commit = _pinned(tmp_path, {"dir/x": "x\n"})
    before = _fingerprint(root)
    _apply(root, commit, "", "case", drafts={"Dir/x": "y\n", new: "n\n"}, deleted=["dir/x"])
    assert _listed(root) == sorted(["Dir", "Dir/x", new])
    local_folder.check_applied(root, "case")
    local_folder.undo_version(root, "case")
    assert _listed(root) == ["dir", "dir/x"]
    assert _fingerprint(root) == before


def test_a_folder_case_rename_moves_the_files_the_copy_leaves_out_and_the_users_files(
    tmp_path: Path, folded_disk: None
) -> None:
    """A .DS_Store and a file the user added in dir/ move with the folder to Dir/, and Undo moves them back."""
    root, commit = _pinned(tmp_path, {"dir/x": "x\n"})
    (root / "dir/.DS_Store").write_bytes(b"\0")
    (root / "dir/notes.txt").write_text("mine\n")
    before = _fingerprint(root)
    _apply(root, commit, "", "case", drafts={"Dir/x": "y\n"}, deleted=["dir/x"])
    assert _listed(root) == ["Dir", "Dir/.DS_Store", "Dir/notes.txt", "Dir/x"]
    local_folder.undo_version(root, "case")
    assert _fingerprint(root) == before


def test_undo_of_a_folder_case_rename_keeps_the_files_added_after_apply(tmp_path: Path, folded_disk: None) -> None:
    """Files that Finder or the user put in Dir/ after Apply do not stop Undo, and move back with the folder."""
    root, commit = _pinned(tmp_path, {"dir/x": "x\n", "keep.txt": "k\n"})
    _apply(root, commit, _FOLDER_CASE_PATCH, "case")
    (root / "Dir/.DS_Store").write_bytes(b"\0")
    (root / "Dir/notes.txt").write_text("mine\n")
    local_folder.undo_version(root, "case")
    assert _listed(root) == ["dir", "dir/.DS_Store", "dir/notes.txt", "dir/x", "keep.txt"]
    assert (root / "dir/x").read_text() == "x\n"
    assert (root / "dir/notes.txt").read_text() == "mine\n"
    local_folder.forget_undone(root, "case")
    assert not local_folder.is_applied("case")


def test_a_failed_write_after_a_folder_case_rename_rolls_the_folder_back(
    tmp_path: Path, folded_disk: None, monkeypatch: pytest.MonkeyPatch
) -> None:
    """A write that fails after dir/ became Dir/ gives back dir/ and dir/x, and keeps no Undo record."""
    root, commit = _pinned(tmp_path, {"dir/x": "x\n", "keep.txt": "k\n"})
    before = _fingerprint(root)
    real = local_folder.os.replace
    failed = []

    def fail_once(src: str, dst: str, *, src_dir_fd: int | None = None, dst_dir_fd: int | None = None) -> None:
        """Fail the first write into the folder, and do every other rename.

        Args:
            src: The name to move.
            dst: The name to replace.
            src_dir_fd: The source's open folder.
            dst_dir_fd: The target's open folder.

        Raises:
            OSError: For the first write into the folder.
        """
        if dst_dir_fd is not None and not failed:
            failed.append(dst)
            raise OSError(errno.ENOSPC, "No space left on device")
        real(src, dst, src_dir_fd=src_dir_fd, dst_dir_fd=dst_dir_fd)

    monkeypatch.setattr(local_folder.os, "replace", fail_once)
    with pytest.raises(LocalFolderError, match="No space left"):
        _apply(root, commit, _FOLDER_CASE_PATCH, "case")
    assert failed == ["x"]
    assert _listed(root) == ["dir", "dir/x", "keep.txt"]
    assert _fingerprint(root) == before
    assert not local_folder.is_applied("case")


def test_undo_of_a_folder_case_rename_that_stopped_half_way_runs_again(
    tmp_path: Path, folded_disk: None, monkeypatch: pytest.MonkeyPatch
) -> None:
    """When Undo stops after it deletes Dir/x, a second Undo still gives back dir/ and dir/x."""
    root, commit = _pinned(tmp_path, {"dir/x": "x\n", "keep.txt": "k\n"})
    before = _fingerprint(root)
    _apply(root, commit, _FOLDER_CASE_PATCH, "case")
    real = local_folder._put
    calls = []

    def fail_second(*arguments: object, **options: object) -> None:
        """Do the first put, and fail the second.

        Args:
            *arguments: The ``_put`` arguments.
            **options: The ``_put`` options.

        Raises:
            OSError: For the second put.
        """
        calls.append(arguments[1])
        if len(calls) == 2:
            raise OSError(errno.ENOSPC, "No space left on device")
        real(*arguments, **options)  # type: ignore[arg-type]

    monkeypatch.setattr(local_folder, "_put", fail_second)
    with pytest.raises(LocalFolderError, match="No space left"):
        local_folder.undo_version(root, "case")
    assert calls == ["Dir/x", "dir/x"]
    monkeypatch.setattr(local_folder, "_put", real)
    local_folder.undo_version(root, "case")
    assert _listed(root) == ["dir", "dir/x", "keep.txt"]
    assert _fingerprint(root) == before
    local_folder.forget_undone(root, "case")
    assert not local_folder.is_applied("case")


def test_undo_of_a_folder_case_rename_after_the_user_deletes_the_new_file(tmp_path: Path, folded_disk: None) -> None:
    """When the user deletes Dir/x after Apply, Undo still gives back dir/ and dir/x."""
    root, commit = _pinned(tmp_path, {"dir/x": "x\n", "keep.txt": "k\n"})
    before = _fingerprint(root)
    _apply(root, commit, _FOLDER_CASE_PATCH, "case")
    (root / "Dir/x").unlink()
    local_folder.undo_version(root, "case")
    assert _listed(root) == ["dir", "dir/x", "keep.txt"]
    assert _fingerprint(root) == before
    local_folder.forget_undone(root, "case")
    assert not local_folder.is_applied("case")


_NESTED_CASE_PATCH = (
    "diff --git a/src/pkg/m.py b/src/pkg/m.py\ndeleted file mode 100644\n--- a/src/pkg/m.py\n+++ /dev/null\n"
    "@@ -1 +0,0 @@\n-m\n"
    "diff --git a/Src/pkg/m.py b/Src/pkg/m.py\nnew file mode 100644\n--- /dev/null\n+++ b/Src/pkg/m.py\n"
    "@@ -0,0 +1 @@\n+m\n"
)


@pytest.mark.parametrize("by", ["patch", "draft"])
def test_apply_and_undo_a_case_rename_of_a_folder_above_another_folder(
    tmp_path: Path, folded_disk: None, by: str
) -> None:
    """src/pkg/m.py to Src/pkg/m.py renames src/ and keeps pkg/ in it; Undo renames it back."""
    root, commit = _pinned(tmp_path, {"src/pkg/m.py": "m\n"})
    before = _fingerprint(root)
    if by == "patch":
        _apply(root, commit, _NESTED_CASE_PATCH, "case")
    else:
        _apply(root, commit, "", "case", drafts={"Src/pkg/m.py": "m\n"}, deleted=["src/pkg/m.py"])
    assert _listed(root) == ["Src", "Src/pkg", "Src/pkg/m.py"]
    local_folder.undo_version(root, "case")
    assert _fingerprint(root) == before


_NEW_FILE_BELOW_A_FILE = {
    "rename-and-new-folder": (
        "diff --git a/a.txt b/A.txt\nsimilarity index 100%\nrename from a.txt\nrename to A.txt\n"
        "diff --git a/sub/f b/sub/f\nnew file mode 100644\n--- /dev/null\n+++ b/sub/f\n@@ -0,0 +1 @@\n+f\n"
    ),
    "below-a-file": (
        "diff --git a/a.txt/b b/a.txt/b\nnew file mode 100644\n--- /dev/null\n+++ b/a.txt/b\n@@ -0,0 +1 @@\n+b\n"
    ),
}


@pytest.mark.parametrize("patch", _NEW_FILE_BELOW_A_FILE.values(), ids=_NEW_FILE_BELOW_A_FILE.keys())
def test_a_patch_that_puts_a_file_below_a_kept_file_does_not_apply(
    tmp_path: Path, folded_disk: None, patch: str
) -> None:
    """A new sub/f or a.txt/b where that name is a file is refused as a patch that does not apply, also in the zip."""
    root, commit = _pinned(tmp_path, {"a.txt": "a\n", "sub": "s\n"})
    before = _fingerprint(root)
    with pytest.raises(LocalFolderError, match=NOT_CLEAN):
        _apply(root, commit, patch, "case")
    assert _fingerprint(root) == before
    assert not local_folder.is_applied("case")
    workdir = tmp_path / "zip"
    workdir.mkdir()
    with pytest.raises(LocalFolderError, match=NOT_CLEAN):
        local_folder.snapshot_zip(commit, patch, workdir, folder="app")


_NEW_FILE_AT_AN_EMPTY_FOLDER = {
    "file": "diff --git a/e b/e\nnew file mode 100644\n--- /dev/null\n+++ b/e\n@@ -0,0 +1 @@\n+f\n",
    "file-under-another-form": "diff --git a/E b/E\nnew file mode 100644\n--- /dev/null\n+++ b/E\n@@ -0,0 +1 @@\n+f\n",
    "folder-under-another-form": (
        "diff --git a/E/x b/E/x\nnew file mode 100644\n--- /dev/null\n+++ b/E/x\n@@ -0,0 +1 @@\n+f\n"
    ),
}


@pytest.mark.parametrize("added", _NEW_FILE_AT_AN_EMPTY_FOLDER.values(), ids=_NEW_FILE_AT_AN_EMPTY_FOLDER.keys())
def test_a_case_rename_that_clashes_with_a_kept_empty_folder_is_refused(
    tmp_path: Path, folded_disk: None, added: str
) -> None:
    """A rename of a.txt to A.txt next to a new e, E or E/x where the kept tree has an empty e/ is refused."""
    root, _ = _pinned(tmp_path, {"a.txt": "a\n"})
    (root / "e").mkdir()
    workdir = tmp_path / "pinned"
    workdir.mkdir()
    commit = local_folder.snapshot_folder(root, workdir).commit
    patch = _CASE_RENAME_PATCHES["rename"] + added
    before = _fingerprint(root)
    with pytest.raises(LocalFolderError, match=NOT_CLEAN):
        _apply(root, commit, patch, "case")
    assert _listed(root) == ["a.txt", "e"]
    assert _fingerprint(root) == before
    assert not local_folder.is_applied("case")
    zipped = tmp_path / "zip"
    zipped.mkdir()
    with pytest.raises(LocalFolderError, match=NOT_CLEAN):
        local_folder.snapshot_zip(commit, patch, zipped, folder="app")


def test_a_case_rename_next_to_a_kept_empty_folder_applies(tmp_path: Path, folded_disk: None) -> None:
    """A rename of a.txt to A.txt and a new e/x into the kept empty e/ applies, and Undo gives the folder back."""
    root, _ = _pinned(tmp_path, {"a.txt": "a\n"})
    (root / "e").mkdir()
    workdir = tmp_path / "pinned"
    workdir.mkdir()
    commit = local_folder.snapshot_folder(root, workdir).commit
    before = _fingerprint(root)
    added = "diff --git a/e/x b/e/x\nnew file mode 100644\n--- /dev/null\n+++ b/e/x\n@@ -0,0 +1 @@\n+f\n"
    _apply(root, commit, _CASE_RENAME_PATCHES["rename"] + added, "case")
    assert _listed(root) == ["A.txt", "e", "e/x"]
    local_folder.undo_version(root, "case")
    assert _listed(root) == ["a.txt", "e"]
    assert _fingerprint(root) == before


_INDEX_REFUSALS = {
    "two-forms": (
        "diff --git a/A.txt b/A.txt\nnew file mode 100644\n--- /dev/null\n+++ b/A.txt\n@@ -0,0 +1 @@\n+z\n",
        NOT_CLEAN,
    ),
    "bad-hunk": ("diff --git a/a.txt b/a.txt\n--- a/a.txt\n+++ b/a.txt\n@@ -1 +1 @@\n-zzz\n+b\n", NOT_CLEAN),
    "symlink": (
        "diff --git a/a.txt b/a.txt\ndeleted file mode 100644\n--- a/a.txt\n+++ /dev/null\n@@ -1 +0,0 @@\n-a\n"
        "diff --git a/A.txt b/A.txt\nnew file mode 120000\n--- /dev/null\n+++ b/A.txt\n@@ -0,0 +1 @@\n"
        "+/etc/passwd\n\\ No newline at end of file\n",
        "a link or a special file at A.txt",
    ),
    "gitlink": (
        "diff --git a/a.txt b/a.txt\ndeleted file mode 100644\n--- a/a.txt\n+++ /dev/null\n@@ -1 +0,0 @@\n-a\n"
        "diff --git a/A.txt b/A.txt\nnew file mode 160000\nindex 0000000..1111111111111111111111111111111111111111\n--- /dev/null\n+++ b/A.txt\n"
        "@@ -0,0 +1 @@\n+Subproject commit 1111111111111111111111111111111111111111\n",
        "a link or a special file at A.txt",
    ),
}


@pytest.mark.parametrize(("patch", "message"), _INDEX_REFUSALS.values(), ids=_INDEX_REFUSALS.keys())
def test_the_index_path_refuses_a_patch_it_cannot_write(
    tmp_path: Path, folded_disk: None, patch: str, message: str
) -> None:
    """A new A.txt next to a kept a.txt, a hunk that does not match, a new link or a gitlink leaves the folder as it was."""
    root, commit = _pinned(tmp_path, {"a.txt": "a\n"})
    before = _fingerprint(root)
    with pytest.raises(LocalFolderError, match=message):
        _apply(root, commit, patch, "case")
    assert _fingerprint(root) == before
    assert not local_folder.is_applied("case")


@pytest.mark.parametrize("command", ["init", "hash-object", "update-index", "apply", "ls-files", "cat-file"])
def test_the_index_path_gives_back_git_applys_error_when_a_git_step_fails(
    tmp_path: Path, folded_disk: None, monkeypatch: pytest.MonkeyPatch, command: str
) -> None:
    """When a git step on the scratch index fails, the build raises the first apply's error."""
    root, commit = _pinned(tmp_path, {"a.txt": "a\n"})
    real = subprocess.run

    def fail(arguments: list[str], **options: object) -> subprocess.CompletedProcess[bytes]:
        """Fail the one git step on the scratch index, and run every other command.

        Args:
            arguments: The command.
            **options: The ``subprocess.run`` options.

        Returns:
            The finished command, or a failed one for the step.
        """
        if command in arguments and any(argument.startswith("--git-dir=") for argument in arguments):
            return subprocess.CompletedProcess(arguments, 1, b"", b"")
        return real(arguments, **options)  # type: ignore[call-overload,no-any-return]

    before = _fingerprint(root)
    monkeypatch.setattr(local_folder.subprocess, "run", fail)
    with pytest.raises(LocalFolderError, match=NOT_CLEAN):
        _apply(root, commit, _CASE_RENAME_PATCHES["rename"], "case")
    assert _listed(root) == ["a.txt"]
    assert _fingerprint(root) == before
    assert not local_folder.is_applied("case")


@pytest.mark.parametrize(
    ("ignores_case", "refusal"),
    [(False, f"{NOT_CLEAN}: error: patch failed"), (True, "Symlinks are not allowed; the change leaves one at 'a'.")],
    ids=["disk-keeps-case", "other-refusal"],
)
def test_the_build_skips_the_index_path_unless_git_apply_refused_on_a_disk_that_ignores_case(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, ignores_case: bool, refusal: str
) -> None:
    """A disk that keeps case, or a refusal other than git apply's, raises the first error with no second try."""
    root, commit = _pinned(tmp_path, {"a.txt": "a\n"})

    def refuse(checkout: Path, patch: str) -> None:
        """Refuse every patch with the test's error.

        Args:
            checkout: The unpacked tree.
            patch: The version's patch.

        Raises:
            RuntimeError: Always.
        """
        raise RuntimeError(refusal)

    monkeypatch.setattr(local_folder, "apply_patch", refuse)
    monkeypatch.setattr(local_folder, "case_insensitive", lambda directory: ignores_case)
    monkeypatch.setattr(local_folder, "_apply_in_index", lambda checkout, patch: pytest.fail("index path used"))
    with pytest.raises(LocalFolderError) as refused:
        _apply(root, commit, _CASE_RENAME_PATCHES["rename"], "case")
    assert str(refused.value) == refusal


def test_an_exact_delete_does_not_take_the_file_under_another_folder_form(tmp_path: Path, folded_disk: None) -> None:
    """A delete of dir/x for a version with twins does not delete Dir/x, which the disk would open for it."""
    root = _write(tmp_path / "app", {"Dir/x": "x\n"})
    blob = local_folder._Blob(hashlib.sha256(b"x\n").hexdigest(), executable=False)
    folder = local_folder._open_root(root)
    try:
        with pytest.raises(local_folder.LocalFolderConflictError, match="dir/x"):
            local_folder._put(folder, "dir/x", blob, None, None, [], exact=True)
    finally:
        os.close(folder)
    assert _listed(root) == ["Dir", "Dir/x"]


def test_git_applys_refusal_starts_with_the_text_the_index_path_looks_for(tmp_path: Path) -> None:
    """The real apply_patch error starts with NOT_CLEAN, so the fallback sees git apply refuse a patch."""
    (tmp_path / "a.txt").write_text("a\n")
    with pytest.raises(RuntimeError) as refused:
        apply_patch(tmp_path, _INDEX_REFUSALS["bad-hunk"][0])
    assert str(refused.value).startswith(f"{NOT_CLEAN}: ")


def test_apply_refuses_a_draft_that_another_case_of_a_new_folder_file_would_take(
    tmp_path: Path, folded_disk: None
) -> None:
    """A Foo.py the user added since the pack is not overwritten by a new draft foo.py."""
    root, commit = _pinned(tmp_path, {"keep.txt": "k\n"})
    (root / "Foo.py").write_text("mine\n")
    with pytest.raises(local_folder.LocalFolderConflictError, match=r"foo\.py"):
        _apply(root, commit, "", "case", drafts={"foo.py": "new\n"})
    assert _listed(root) == ["Foo.py", "keep.txt"]
    assert (root / "Foo.py").read_text() == "mine\n"
    assert not local_folder.is_applied("case")


def test_apply_refuses_a_case_rename_when_the_user_added_a_third_form(tmp_path: Path, folded_disk: None) -> None:
    """A FOO.py the user put in place of Foo.py is not one of the rename's two forms, so it is a conflict."""
    root, commit = _pinned(tmp_path, {"Foo.py": "old\n"})
    (root / "Foo.py").unlink()
    (root / "FOO.py").write_text("mine\n")
    with pytest.raises(local_folder.LocalFolderConflictError):
        _apply(root, commit, "", "case", drafts={"foo.py": "new\n"}, deleted=["Foo.py"])
    assert _listed(root) == ["FOO.py"]
    assert (root / "FOO.py").read_text() == "mine\n"
    assert not local_folder.is_applied("case")
