"""Tests for packing a folder on this computer as a repository run's source (SKY-990)."""

from __future__ import annotations

import hashlib
import io
import os
import tarfile
import threading
import unicodedata
import zipfile
from collections.abc import Callable
from pathlib import Path

import pytest

from core.config import settings
from core.local import folder_tree
from core.service_gateway.optimization.blackbox.repo_tree import case_insensitive
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
