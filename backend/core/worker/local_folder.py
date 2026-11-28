"""Pack a folder on this computer as a repository run's source, with no git (SKY-990).

The desktop local stack can run on a folder instead of a hosted repository.
The folder is packed into the same ``tree.tgz`` a git snapshot ships, so the
rest of staging (drafts, hidden-path strip, gates) runs unchanged. A content
hash of the kept files stands in for the commit id (``folder_tree.folder_source_id``),
and a copy is kept under ``settings.local_state_dir`` for the run-detail file
browser and for Apply/Undo (SKY-991). Only Apply and Undo write the folder,
and nothing leaves this computer: only the local ``docker`` provider may run a
local source.
"""

from __future__ import annotations

import base64
import contextlib
import gzip
import hashlib
import io
import json
import os
import re
import secrets
import shutil
import stat
import subprocess
import tarfile
import tempfile
import threading
import time
import zipfile
from collections.abc import Collection, Iterable, Mapping
from dataclasses import dataclass
from pathlib import Path
from typing import Any, BinaryIO, Literal

from ..config import settings
from ..connectors.github_repo import RepoFetchError, RepoSnapshot
from ..connectors.repo_drafts import MAX_ARCHIVE_BYTES, DraftFileError, write_to_checkout
from ..local.folder_tree import (
    EXEC_MARKER,
    SKIPPED_DIRS,
    FolderTreeError,
    folder_entries,
    is_env_name,
    is_executable,
    is_os_name,
    open_no_follow,
    source_id,
)
from ..models.blackbox import LOCAL_FOLDER_ONLY_HERE, LOCAL_REPO_PROVIDER, REPO_COMMIT_PATTERN
from ..service_gateway.optimization.blackbox.repo_tree import (
    NOT_CLEAN,
    SAFE_GIT,
    apply_patch,
    case_insensitive,
    fold_case,
    inside,
    patch_paths,
    with_git_headers,
)

LOCAL_PROVIDER = LOCAL_REPO_PROVIDER
LOCAL_SANDBOX_PROVIDER = "docker"
KEPT_FOLDER = "local-folders"
APPLIED_FOLDER = "local-applies"
_LARGEST_SHOWN = 5
# A cleanup reads the stored runs before it deletes a pack. A run that wrote or reused the pack
# since then stores its commit only later, so a pack this recent is never deleted (SKY-1005).
FRESH_PACK_SECONDS = 15 * 60
# Only the execute bit reaches the pack and the id, so umask and group bits do not move them.
_DIR_MODE = 0o755
_EXEC_MODE = 0o755
_FILE_MODE = 0o644
_NO_FOLLOW = getattr(os, "O_NOFOLLOW", 0)
_FOLDER_FLAGS = os.O_RDONLY | getattr(os, "O_DIRECTORY", 0) | _NO_FOLLOW
# One Apply or Undo at a time on this computer, so two clicks cannot interleave their writes.
# The router also holds it while it saves the run's state, so the state follows the folder's order.
APPLY_LOCK = threading.RLock()
# A blocked path matches in every case and Unicode form, as on the macOS disk of the desktop app.
# On a case-sensitive disk this only refuses more, and no probe file goes into the user's folder.
_FOLD_BLOCKED = fold_case(True)
_FOLD_EDITABLE = fold_case(False)
_GIT_FILE_MODES = {"100644": _FILE_MODE, "100755": _EXEC_MODE}


class LocalFolderError(RepoFetchError):
    """Explain why a local folder cannot be packed or read as a run's source."""


class _Tee:
    """Hash a file's bytes while the pack copies them, and stop when the archive is too large."""

    def __init__(self, body: io.BufferedReader, digest: Any, raw: BinaryIO, entries: list[dict[str, Any]]) -> None:
        """Wrap one open file.

        Args:
            body: The file, open for reading.
            digest: The running SHA-1 of the bytes read so far.
            raw: The archive file that the pack writes.
            entries: The folder's listing, to name its largest folders in the error.
        """
        self._body = body
        self._digest = digest
        self._raw = raw
        self._entries = entries

    def read(self, size: int = -1) -> bytes:
        """Read the next bytes for the pack and add them to the digest.

        Args:
            size: The most bytes to read; ``-1`` reads to the end.

        Returns:
            The bytes read.

        Raises:
            LocalFolderError: When the archive grew past ``MAX_ARCHIVE_BYTES``.
        """
        # Stopping early spares packing a huge file only to refuse it.
        if self._raw.tell() > MAX_ARCHIVE_BYTES:
            raise LocalFolderError(_too_large(self._entries))
        data = self._body.read(size)
        self._digest.update(data)
        return data


def is_local(repo: Mapping[str, Any] | None) -> bool:
    """Check whether a ``target.repo`` block names a local folder.

    Args:
        repo: The run's repository block, or ``None``.

    Returns:
        True for ``provider: "local"``.
    """
    return isinstance(repo, Mapping) and repo.get("provider") == LOCAL_PROVIDER


def ensure_runs_here(sandbox_provider: str | None) -> None:
    """Refuse a local source unless this is the local stack and the run uses its Docker box.

    Args:
        sandbox_provider: The provider the run names; ``None`` uses the
            deployment default, which is ``docker`` only in local mode.

    Raises:
        LocalFolderError: Outside local mode, or for any other provider.
    """
    if not settings.local_mode or (sandbox_provider or LOCAL_SANDBOX_PROVIDER) != LOCAL_SANDBOX_PROVIDER:
        raise LocalFolderError(LOCAL_FOLDER_ONLY_HERE)


def kept_snapshot(commit: str) -> Path:
    """Return where the packed original of a local run is kept.

    Args:
        commit: The folder's source id, which the run stores as its commit.

    Returns:
        ``<local_state_dir>/local-folders/<commit>.tgz``; the file may not exist.

    Raises:
        LocalFolderError: When the commit is not a SHA-1 or this computer has
            no local state folder.
    """
    if not re.fullmatch(REPO_COMMIT_PATTERN, commit or ""):
        raise LocalFolderError("The local folder's snapshot id is not valid.")
    if not settings.local_state_dir:
        raise LocalFolderError(
            "This computer has no local state folder (APP_LOCAL_STATE_DIR) to keep the folder's copy."
        )
    return Path(settings.local_state_dir) / KEPT_FOLDER / f"{commit}.tgz"


def forget_snapshot(commit: str) -> bool:
    """Delete the kept pack of a commit that no stored run uses any more (SKY-1005).

    A pack written or reused in the last ``FRESH_PACK_SECONDS`` stays.

    Args:
        commit: The folder's source id.

    Returns:
        Whether the pack was deleted; False when it is missing or recent.

    Raises:
        LocalFolderError: When the commit is not a SHA-1, there is no local
            state folder, or the pack cannot be deleted.
    """
    kept = kept_snapshot(commit)
    try:
        if time.time() - kept.stat().st_mtime < FRESH_PACK_SECONDS:
            return False
        kept.unlink()
    except FileNotFoundError:
        return False
    except OSError as error:
        raise LocalFolderError(f"The kept folder copy could not be deleted: {error.strerror or error}") from None
    return True


def snapshot_folder(path: str | os.PathLike[str], workdir: Path, commit: str | None = None) -> RepoSnapshot:
    """Pack a local folder into ``workdir/tree.tgz`` and keep its original copy.

    A run that already pinned a commit reuses its kept copy, so a resumed run
    sees the tree it started from, even after the folder changed.

    Args:
        path: The absolute folder path.
        workdir: Parent-owned folder the archive goes in.
        commit: The pinned source id, or ``None`` for a new run.

    Returns:
        The snapshot; its commit is the folder's ``folder_tree.folder_source_id``.

    Raises:
        LocalFolderError: When the folder is missing or unreadable, the pack
            is larger than ``MAX_ARCHIVE_BYTES``, or the folder no longer
            matches the pinned commit and no kept copy exists.
    """
    archive = workdir / "tree.tgz"
    if commit:
        kept = kept_snapshot(commit)
        # A cleanup can delete the pack between the check and the copy; then the folder is packed again.
        with contextlib.suppress(FileNotFoundError):
            if kept.is_file():
                # The fresh mtime tells a cleanup that this pack is in use again.
                os.utime(kept)
                shutil.copyfile(kept, archive)
                return RepoSnapshot(
                    commit=commit, archive=archive, readonly_paths=(), size_bytes=archive.stat().st_size
                )
    root = Path(path)
    try:
        entries = folder_entries(root, limit=None)[0]
        digest = _pack(root, entries, archive)
    except FolderTreeError as error:
        raise LocalFolderError(str(error)) from None
    except OSError as error:
        archive.unlink(missing_ok=True)
        raise LocalFolderError(f"The folder {root} could not be read: {error.strerror or error}") from None
    except BaseException:
        archive.unlink(missing_ok=True)
        raise
    if commit and digest != commit:
        archive.unlink()
        raise LocalFolderError("The folder changed after the run was set up. Set up the run again.")
    _keep(archive, digest)
    return RepoSnapshot(commit=digest, archive=archive, readonly_paths=(), size_bytes=archive.stat().st_size)


def snapshot_tree(commit: str) -> dict[str, Any]:
    """List a local run's kept tree, in the shape a host's ``tree_entries`` returns.

    Args:
        commit: The run's pinned source id.

    Returns:
        ``{"entries": [{"path", "type": "file" | "dir", "size"?}], "truncated": False}``.

    Raises:
        LocalFolderError: When the kept copy is missing or unreadable.
    """
    entries = []
    for member in _members(commit):
        if member.isdir():
            entries.append({"path": member.name, "type": "dir"})
        elif member.isreg():
            entries.append({"path": member.name, "type": "file", "size": member.size})
    return {"entries": entries, "truncated": False}


def snapshot_file(commit: str, path: str, max_bytes: int) -> dict[str, Any]:
    """Read one file of a local run's kept tree, in the shape a host's ``file_at`` returns.

    Args:
        commit: The run's pinned source id.
        path: The cleaned repository path.
        max_bytes: Files larger than this are reported but not read.

    Returns:
        ``{"content": bytes | None, "size": int | None, "missing": bool, "too_large": bool}``;
        ``missing`` covers a path that is absent or not a regular file.

    Raises:
        LocalFolderError: When the kept copy is missing or unreadable.
    """
    with _open_kept(commit) as packed:
        try:
            member = packed.getmember(path)
        except KeyError:
            member = None
        if member is None or not member.isreg():
            return {"content": None, "size": None, "missing": True, "too_large": False}
        if member.size > max_bytes:
            return {"content": None, "size": member.size, "missing": False, "too_large": True}
        handle = packed.extractfile(member)
        content = handle.read() if handle is not None else b""
    return {"content": content, "size": member.size, "missing": False, "too_large": False}


def snapshot_zip(
    commit: str,
    patch: str,
    workdir: Path,
    *,
    folder: str,
    drafts: Mapping[str, str] | None = None,
    readonly_paths: Iterable[str] = (),
    deleted: Iterable[str] = (),
) -> Path:
    """Zip a local run's kept tree with the drafts written and one version's patch applied.

    Args:
        commit: The run's pinned source id.
        patch: A unified diff against the kept tree; empty for the tree as is.
        workdir: Empty folder the checkout and zip go in.
        folder: The one top-level folder name inside the zip.
        drafts: The run's ``draft_files``, which the patch was made against.
        readonly_paths: Paths a draft cannot change.
        deleted: The run's ``draft_deleted``, removed with the drafts.

    Returns:
        The zip, its entries under ``<folder>/``, like a host run's zip.

    Raises:
        LocalFolderError: When the kept copy is missing, a draft cannot be
            written, the patch does not apply, or the zip is larger than the limit.
    """
    checkout = workdir / "checkout"
    _build_version(commit, patch, checkout, drafts, readonly_paths, deleted)
    archive = workdir / f"{folder}.zip"
    # The pack stores every time as 0, which a zip cannot hold; this writes 1980 instead.
    with zipfile.ZipFile(archive, "w", zipfile.ZIP_DEFLATED, strict_timestamps=False) as zipped:
        for item in sorted(checkout.rglob("*")):
            if item.is_symlink() or not item.is_file():
                continue
            zipped.write(item, f"{folder}/{item.relative_to(checkout).as_posix()}")
    shutil.rmtree(checkout, ignore_errors=True)
    if archive.stat().st_size > MAX_ARCHIVE_BYTES:
        archive.unlink()
        raise LocalFolderError(f"The folder is larger than {MAX_ARCHIVE_BYTES // (1024 * 1024)} MB compressed.")
    return archive


class LocalFolderConflictError(LocalFolderError):
    """Name the folder's files that changed, so Apply or Undo wrote nothing."""

    def __init__(self, paths: Iterable[str]) -> None:
        """Record the changed files.

        Args:
            paths: The files, as ``/``-joined paths below the folder.
        """
        self.paths = sorted(paths)
        super().__init__(f"These files changed in the folder: {', '.join(self.paths)}")


@dataclass(frozen=True)
class _Blob:
    """One regular file's content hash and execute bit."""

    sha256: str
    executable: bool


# The state of one path in the user's folder: absent, a regular file, or anything else (a link, a folder, a pipe).
_Disk = _Blob | Literal["other"] | None


@dataclass
class _Change:
    """One file that Apply changes: its state before, its state after, and the bytes Undo restores."""

    path: str
    before: _Blob | None
    after: _Blob | None
    before_data: bytes | None = None
    before_mode: int | None = None


def applied_record(run_id: str) -> Path:
    """Return where Undo's record of one run's Apply is kept.

    Args:
        run_id: The run.

    Returns:
        ``<local_state_dir>/local-applies/<run_id>.json``; the file may not exist.

    Raises:
        LocalFolderError: When the run id is not a plain name, or this
            computer has no local state folder.
    """
    if not re.fullmatch(r"[A-Za-z0-9_-]{1,128}", run_id or ""):
        raise LocalFolderError("The run id is not valid.")
    if not settings.local_state_dir:
        raise LocalFolderError("This computer has no local state folder (APP_LOCAL_STATE_DIR) to keep the Undo copy.")
    return Path(settings.local_state_dir) / APPLIED_FOLDER / f"{run_id}.json"


def is_applied(run_id: str) -> bool:
    """Check whether a run's version is in its folder, so Undo can take it back.

    Args:
        run_id: The run.

    Returns:
        True while the Undo record of the run's Apply exists.

    Raises:
        LocalFolderError: When the run id is not valid or there is no local state folder.
    """
    return applied_record(run_id).is_file()


def apply_version(
    root: str | os.PathLike[str],
    commit: str,
    patch: str,
    run_id: str,
    *,
    drafts: Mapping[str, str] | None = None,
    readonly_paths: Iterable[str] = (),
    editable_paths: Iterable[str],
    blocked_paths: Iterable[str],
    deleted: Collection[str] = (),
) -> list[str]:
    """Write one version of a local run into its folder, and keep what Undo needs.

    The version is the kept tree with the drafts and the patch on it. Every
    file it changes, adds or deletes must still match the kept tree in the
    folder; else nothing is written. Each file is written to a temporary file
    in its own folder and renamed over the old one, and no link on the way is
    followed, so a folder that changes during Apply cannot send a write
    outside it.

    Args:
        root: The run's folder.
        commit: The run's pinned source id.
        patch: The version's patch against the kept tree.
        run_id: The run, which names the Undo record.
        drafts: The run's ``draft_files``.
        readonly_paths: Paths a draft cannot change.
        editable_paths: The run's editable paths; every file the patch changes must lie below one.
        blocked_paths: Read-only, protected and scoring-side paths; no file
            the patch changes may lie below one, in any case or Unicode form.
        deleted: The run's ``draft_deleted``, removed with the drafts.

    Returns:
        The changed files, sorted.

    Raises:
        LocalFolderConflictError: When a file to change no longer matches the kept tree.
        LocalFolderError: When the version is already applied, the kept copy
            is missing, the version cannot be built, it changes a path the
            folder copy leaves out or the run's rules forbid, or a write
            fails. Files already written are put back first; the Undo record
            stays when one of them could not be put back.
    """
    with APPLY_LOCK:
        record = applied_record(run_id)
        if record.is_file():
            raise LocalFolderError("The best version is already in the folder.")
        workdir = Path(tempfile.mkdtemp(prefix="local-apply-"))
        try:
            original, checkout = workdir / "original", workdir / "checkout"
            original.mkdir()
            with _open_kept(commit) as packed:
                packed.extractall(original, filter="data")
            drafted = _build_version(commit, patch, checkout, drafts, readonly_paths, deleted)
            changes = _changes(_scan(original), _scan(checkout))
            _check_rules(changes, drafted, editable_paths, blocked_paths, deleted)
            # Deletes go first, so on a disk that ignores case a deleted Foo.py is gone
            # before its drafted foo.py is written; Undo takes them back in reverse.
            changes.sort(key=lambda change: change.after is not None)
            # Before the writes, a file under another form may only be one this Apply deletes.
            pending = _twins(changes, deletes_only=True)
            folder = _open_root(root)
            try:
                conflicts = []
                for change in changes:
                    disk, data, mode = _read_disk(folder, change.path, pending.get(change.path))
                    if disk != change.before:
                        conflicts.append(change.path)
                    change.before_data, change.before_mode = data, mode
                if conflicts:
                    raise LocalFolderConflictError(conflicts)
                made: list[str] = []
                _save_record(record, root, changes, made)
                done: list[_Change] = []
                try:
                    for change in changes:
                        data = (checkout / change.path).read_bytes() if change.after else None
                        mode = _mode_for(change.after, change.before_mode)
                        known = len(made)
                        _put(folder, change.path, change.before, data, mode, made, exact=change.path in pending)
                        done.append(change)
                        if len(made) != known:
                            # Undo must find every folder Apply made, also after a crash.
                            _save_record(record, root, changes, made)
                except BaseException:
                    # A file left half way still needs its kept bytes, so only a full rollback drops the record.
                    if _put_back(folder, done, made):
                        record.unlink(missing_ok=True)
                    raise
            finally:
                os.close(folder)
        except OSError as error:
            raise LocalFolderError(f"The folder could not be written: {error.strerror or error}") from None
        finally:
            shutil.rmtree(workdir, ignore_errors=True)
    return sorted(change.path for change in changes)


def undo_version(root: str | os.PathLike[str], run_id: str) -> list[str]:
    """Put back every file that Apply changed, from the bytes Apply kept, and delete the files it added.

    A file that is already back in its state before Apply is left as it is,
    so an Undo that stopped half way can run again. The Undo record stays:
    the caller deletes it with ``forget_apply`` once it saves the run as not
    applied, so a failed save still leaves a working Undo.

    Args:
        root: The run's folder.
        run_id: The run.

    Returns:
        The restored files, sorted.

    Raises:
        LocalFolderConflictError: When a file changed after Apply; nothing is restored then.
        LocalFolderError: When the version is not applied, the Undo record
            cannot be read or is for another folder, or a write fails.
    """
    with APPLY_LOCK:
        record = applied_record(run_id)
        changes, made = _load_record(record, root)
        try:
            folder = _open_root(root)
            try:
                pending = []
                conflicts = []
                twins = _twins(changes)
                for change in changes:
                    disk = _read_disk(folder, change.path, twins.get(change.path))[0]
                    if disk == change.before:
                        continue
                    if disk != change.after:
                        conflicts.append(change.path)
                    pending.append(change)
                if conflicts:
                    raise LocalFolderConflictError(conflicts)
                _put_back(folder, pending, made, strict=True)
            finally:
                os.close(folder)
        except OSError as error:
            raise LocalFolderError(f"The folder could not be written: {error.strerror or error}") from None
    return [change.path for change in changes]


def forget_apply(run_id: str) -> None:
    """Delete the Undo record of a run's Apply after its Undo.

    Args:
        run_id: The run.

    Raises:
        LocalFolderError: When the run id is not valid, there is no local
            state folder, or the record cannot be deleted.
    """
    with APPLY_LOCK:
        try:
            applied_record(run_id).unlink(missing_ok=True)
        except OSError as error:
            raise LocalFolderError(f"The Undo record could not be deleted: {error.strerror or error}") from None


def forget_undone(root: str | os.PathLike[str], run_id: str) -> None:
    """Delete an Undo record whose Undo finished, when every file is back in its state before Apply.

    A record delete that fails after a finished Undo leaves the record. Any
    other folder state keeps it, so a half-applied or edited folder still
    stops a repeat Apply.

    Args:
        root: The run's folder.
        run_id: The run.

    Raises:
        LocalFolderError: When the record is missing, damaged or for another
            folder, the folder cannot be read, or the record cannot be deleted.
    """
    with APPLY_LOCK:
        record = applied_record(run_id)
        changes = _load_record(record, root)[0]
        try:
            folder = _open_root(root)
        except OSError as error:
            raise LocalFolderError(f"The folder could not be read: {error.strerror or error}") from None
        try:
            twins = _twins(changes)
            undone = all(
                _read_disk(folder, change.path, twins.get(change.path))[0] == change.before for change in changes
            )
        finally:
            os.close(folder)
        if undone:
            try:
                record.unlink(missing_ok=True)
            except OSError as error:
                raise LocalFolderError(f"The Undo record could not be deleted: {error.strerror or error}") from None


def check_applied(root: str | os.PathLike[str], run_id: str) -> None:
    """Check that every file a run's Apply changes holds the version, not only that the Undo record exists.

    A failed rollback keeps the record on purpose, and a stop during Apply
    leaves it too, so the folder can hold only part of the version.

    Args:
        root: The run's folder.
        run_id: The run.

    Raises:
        LocalFolderError: When the record is missing, damaged or for another
            folder, the folder cannot be read, or a file does not hold the version.
    """
    with APPLY_LOCK:
        changes = _load_record(applied_record(run_id), root)[0]
        try:
            folder = _open_root(root)
        except OSError as error:
            raise LocalFolderError(f"The folder could not be read: {error.strerror or error}") from None
        try:
            twins = _twins(changes)
            missing = [
                change.path
                for change in changes
                if _read_disk(folder, change.path, twins.get(change.path))[0] != change.after
            ]
        finally:
            os.close(folder)
    if missing:
        raise LocalFolderError(
            f"These files do not hold the best version: {', '.join(missing)}. Undo first, then Apply again."
        )


def _build_version(
    commit: str,
    patch: str,
    checkout: Path,
    drafts: Mapping[str, str] | None,
    readonly_paths: Iterable[str],
    deleted: Iterable[str] = (),
) -> dict[str, _Blob]:
    """Unpack the kept tree into a new folder, then write the drafts and the patch over it.

    Args:
        commit: The run's pinned source id.
        patch: A unified diff against the kept tree; empty for the tree as is.
        checkout: The folder to make; it must not exist.
        drafts: The run's ``draft_files``.
        readonly_paths: Paths a draft cannot change.
        deleted: The run's ``draft_deleted``, removed with the drafts.

    Returns:
        The tree's files after the drafts and before the patch.

    Raises:
        LocalFolderError: When the kept copy is missing, a draft cannot be
            written, or the patch does not apply.
    """
    checkout.mkdir(parents=True)
    with _open_kept(commit) as packed:
        packed.extractall(checkout, filter="data")
    try:
        write_to_checkout(checkout, drafts, readonly_paths, deleted)
        drafted = _scan(checkout) if drafts else {}
        try:
            apply_patch(checkout, patch)
        except RuntimeError as error:
            # git apply refuses A.txt while a.txt is on a disk that ignores case, also when the
            # patch moves a.txt away (SKY-1010). An index keeps every case apart, so try there.
            if not str(error).startswith(NOT_CLEAN) or not case_insensitive(checkout.parent):
                raise
            if not _apply_in_index(checkout, patch):
                raise
    except (DraftFileError, RuntimeError) as error:
        raise LocalFolderError(str(error)) from None
    return drafted


def _apply_in_index(checkout: Path, patch: str) -> bool:
    """Apply a patch in a git index, then write its result into a checkout on a disk that ignores case.

    Only the files the patch names go into the index, by exact name, so a
    case-only rename such as ``a.txt`` to ``A.txt`` or ``dir/x`` to ``Dir/x``
    applies. ``apply_patch`` checked the patch's headers already.

    Args:
        checkout: The unpacked tree, as ``apply_patch`` left it after it refused the patch.
        patch: The version's patch.

    Returns:
        Whether the patch applied; ``False`` leaves the checkout as it was.

    Raises:
        RuntimeError: When the result has a link or another special file.
    """
    git = ["git", *SAFE_GIT, "-c", "core.ignorecase=false", f"--git-dir={checkout.parent / 'index.git'}"]

    def run(*arguments: str, data: bytes = b"") -> subprocess.CompletedProcess[bytes]:
        """Run one git command on the scratch index.

        Args:
            *arguments: The git command and its arguments.
            data: The command's input.

        Returns:
            The finished command.
        """
        return subprocess.run([*git, *arguments], input=data, capture_output=True, check=False)

    if run("init", "--quiet", "--bare").returncode:
        return False
    found = _scan(checkout)
    entries = {}
    for path in patch_paths(with_git_headers(patch)):
        if path in found:
            # --no-filters keeps the bytes exact; a .gitattributes in the tree could change line ends.
            body = run("hash-object", "-w", "--no-filters", "--stdin", data=(checkout / path).read_bytes())
            mode = "100755" if found[path].executable else "100644"
            entries[path] = (mode, body.stdout.decode().strip())
    listing = b"".join(f"{mode} {sha}\t{path}\0".encode() for path, (mode, sha) in entries.items())
    if run("update-index", "--add", "-z", "--index-info", data=listing).returncode:
        return False
    applied = run("apply", "--cached", "--binary", "--whitespace=nowarn", "-", data=with_git_headers(patch).encode())
    if applied.returncode:
        return False
    result = {}
    for line in run("ls-files", "--stage", "-z").stdout.decode().split("\0"):
        if line:
            info, path = line.split("\t", 1)
            mode, sha, _ = info.split(" ")
            if mode not in _GIT_FILE_MODES:
                raise RuntimeError(f"Symlinks are not allowed; the change leaves one at {path!r}.")
            result[path] = (mode, sha)
    gone = entries.keys() - result.keys()
    # The disk keeps one form of each name, so a version with two forms of a path cannot be written.
    # The index holds only the patch's files, so it also misses a new file below a kept file.
    forms: dict[str, set[str]] = {}
    files, folders = set(), set()
    for path in (found.keys() - gone) | result.keys():
        parts = path.split("/")
        files.add(_FOLD_BLOCKED(path))
        for depth in range(1, len(parts) + 1):
            name = "/".join(parts[:depth])
            forms.setdefault(_FOLD_BLOCKED(name), set()).add(name)
            if depth < len(parts):
                folders.add(_FOLD_BLOCKED(name))
    if any(len(names) > 1 for names in forms.values()) or files & folders:
        return False
    blobs: dict[str, tuple[bytes, int]] = {}
    for path, (mode, sha) in result.items():
        if entries.get(path) != (mode, sha):
            blob = run("cat-file", "blob", sha)
            if blob.returncode:
                return False
            blobs[path] = (blob.stdout, _GIT_FILE_MODES[mode])
    for path in gone:
        (checkout / path).unlink()
        folder = (checkout / path).parent
        while folder != checkout and not any(folder.iterdir()):
            folder.rmdir()
            folder = folder.parent
    for path, (data, bits) in blobs.items():
        target = checkout / path
        target.parent.mkdir(parents=True, exist_ok=True)
        target.write_bytes(data)
        target.chmod(bits)
    return True


def _scan(tree: Path) -> dict[str, _Blob]:
    """Hash every regular file of an unpacked tree.

    Args:
        tree: A tree this module unpacked.

    Returns:
        ``/``-joined path to the file's hash and execute bit.

    Raises:
        LocalFolderError: When the tree holds a link or anything other than
            folders and regular files.
    """
    found: dict[str, _Blob] = {}
    for top, folders, files in os.walk(tree):
        for name in [*folders, *files]:
            item = Path(top, name)
            status = item.lstat()
            rel = item.relative_to(tree).as_posix()
            if stat.S_ISDIR(status.st_mode):
                continue
            if not stat.S_ISREG(status.st_mode):
                raise LocalFolderError(f"The version leaves a link or a special file at {rel}.")
            with item.open("rb") as body:
                found[rel] = _Blob(hashlib.file_digest(body, "sha256").hexdigest(), is_executable(status.st_mode))
    return found


def _changes(original: dict[str, _Blob], version: dict[str, _Blob]) -> list[_Change]:
    """List the files a version changes, adds or deletes, compared with the kept tree.

    Args:
        original: The kept tree's files.
        version: The version's files.

    Returns:
        One change per differing path, sorted by path.

    Raises:
        LocalFolderError: When a change is at a path the folder copy leaves
            out (git data, installed packages, ``.env*`` and operating system
            files), which the kept tree cannot vouch for, or when a folder
            becomes a file or a file becomes a folder.
    """
    changes = []
    for path in sorted(original.keys() | version.keys()):
        before, after = original.get(path), version.get(path)
        if before == after:
            continue
        for part in path.split("/"):
            if part.lower() in SKIPPED_DIRS or is_env_name(part) or is_os_name(part):
                raise LocalFolderError(f"The version changes {path}, which the folder copy leaves out.")
        changes.append(_Change(path, before, after))
    # A path and a path below it can both change only when a folder becomes a file or back.
    # The file-by-file writes cannot do that in place, so Apply refuses it before any write.
    # Fold as the disk does, so a file Pkg and a folder pkg/ also count as one path.
    paths = {_FOLD_BLOCKED(change.path) for change in changes}
    for change in changes:
        parts = change.path.split("/")
        for depth in range(1, len(parts)):
            if _FOLD_BLOCKED(parent := "/".join(parts[:depth])) in paths:
                raise LocalFolderError(
                    f"The version turns {parent} from a folder into a file, or back, which Apply cannot do. "
                    "Download the version instead."
                )
    return changes


def _check_rules(
    changes: list[_Change],
    drafted: Mapping[str, _Blob],
    editable_paths: Iterable[str],
    blocked_paths: Iterable[str],
    deleted: Collection[str] = (),
) -> None:
    """Refuse a version whose real changed files break the run's editing rules.

    The check runs on the files the built version changes, not on the names
    in the patch, so a name in another case or Unicode form cannot reach a
    blocked file on the disk. A file that ends as the setup draft wrote it
    is the run's own setup, which a pull request also carries, so only the
    read-only paths, checked when the draft is written, apply to it. A file
    that the setup deleted and the patch did not add back is the same.

    Args:
        changes: The version's changes.
        drafted: The tree's files after the drafts and before the patch.
        editable_paths: Paths the run may change.
        blocked_paths: Paths the run may never change.
        deleted: The run's ``draft_deleted``.

    Raises:
        LocalFolderError: When a changed file is blocked or outside the editable paths.
    """
    editable = [_FOLD_EDITABLE(path) for path in editable_paths]
    blocked = [_FOLD_BLOCKED(path.strip("/")) for path in blocked_paths if path.strip("/")]
    for change in changes:
        if change.after is not None and drafted.get(change.path) == change.after:
            continue
        if change.after is None and change.path in deleted:
            continue
        folded = _FOLD_BLOCKED(change.path)
        if inside(folded, blocked) or any(root.startswith(folded + "/") for root in blocked):
            raise LocalFolderError(f"The version changes {change.path}, which the run may not change.")
        if not inside(_FOLD_EDITABLE(change.path), editable):
            raise LocalFolderError(f"The version changes {change.path}, which is outside the editable paths.")


def _open_root(root: str | os.PathLike[str]) -> int:
    """Open the run's folder for the walks below it, refusing a link.

    Args:
        root: The folder.

    Returns:
        The open folder descriptor; the caller closes it.

    Raises:
        OSError: When the folder is missing, not a folder, or a link.
    """
    return os.open(root, _FOLDER_FLAGS)


def _walk(
    folder: int,
    parts: list[str],
    made: list[str] | None = None,
    prefix: str = "",
    *,
    exact: bool = False,
    names: list[str] | None = None,
) -> int | None:
    """Open the folder at ``parts`` below an open folder, one part at a time, never following a link.

    Args:
        folder: The open folder to start from; it stays open.
        parts: The path parts to walk.
        made: When given, missing folders are made and their paths added to it.
        prefix: The ``/``-joined path of ``folder`` below the root, for ``made``.
        exact: Take a folder only under its exact name. A folder that a disk
            which ignores case opens under another form counts as missing
            without ``made``; with ``made``, it is renamed to the exact name,
            with everything in it.
        names: When given, the name each part has on the disk is added to it.

    Returns:
        The open folder; ``None`` when a part is missing and ``made`` is ``None``.

    Raises:
        OSError: When a part is a link or a file, or cannot be opened, made or renamed.
    """
    current = os.dup(folder)
    try:
        for index, part in enumerate(parts):
            try:
                below = os.open(part, _FOLDER_FLAGS, dir_fd=current)
            except FileNotFoundError:
                if made is None:
                    os.close(current)
                    return None
                os.mkdir(part, _DIR_MODE, dir_fd=current)
                made.append("/".join([*([prefix] if prefix else []), *parts[: index + 1]]))
                below = os.open(part, _FOLDER_FLAGS, dir_fd=current)
            if exact or names is not None:
                name = _disk_name(current, part)
                if exact and name != part:
                    os.close(below)
                    if made is None:
                        os.close(current)
                        return None
                    # A folder rename by case only. The disk keeps one form per folder, so the
                    # folder itself takes the new form, and the files the copy leaves out
                    # (.DS_Store), the user's files and the version's other new files move with it.
                    os.rename(name, part, src_dir_fd=current, dst_dir_fd=current)
                    below = os.open(part, _FOLDER_FLAGS, dir_fd=current)
                    name = part
                if names is not None:
                    names.append(name)
            os.close(current)
            current = below
    except BaseException:
        os.close(current)
        raise
    return current


def _disk_name(folder: int, name: str) -> str:
    """Find the name under which an open folder lists an entry that the disk opens as ``name``.

    Args:
        folder: The open folder.
        name: The name opened in it.

    Returns:
        ``name`` when the folder lists it; else the one other case or Unicode
        form it lists. ``name`` again when it lists no form or more than one.
    """
    # pathlib cannot list a folder descriptor.
    listed = os.listdir(folder)
    if name in listed:
        return name
    folded = _FOLD_BLOCKED(name)
    forms = [entry for entry in listed if _FOLD_BLOCKED(entry) == folded]
    return forms[0] if len(forms) == 1 else name


def _twins(changes: Iterable[_Change], *, deletes_only: bool = False) -> dict[str, set[str]]:
    """Find the changed files that a disk which ignores case or Unicode form can open as one file.

    Args:
        changes: The Apply's changes.
        deletes_only: Count only the other files that the Apply deletes.

    Returns:
        Each such path to the other changed paths that fold to the same path,
        folders included, so ``dir/x`` and ``Dir/x`` are twins.
    """
    groups: dict[str, list[_Change]] = {}
    for change in changes:
        groups.setdefault(_FOLD_BLOCKED(change.path), []).append(change)
    return {
        change.path: {
            other.path for other in group if other is not change and (not deletes_only or other.after is None)
        }
        for group in groups.values()
        if len(group) > 1
        for change in group
    }


def _read_disk(folder: int, path: str, twins: Collection[str] | None = None) -> tuple[_Disk, bytes | None, int | None]:
    """Read one path of the user's folder without following a link.

    Args:
        folder: The open root folder.
        path: The ``/``-joined path below it.
        twins: None to read the path as the disk opens it. Else only the exact
            path counts, and a file whose path on the disk has another case or
            Unicode form, in its name or a folder, reads as missing only when
            that path is in ``twins``; any other form reads as ``"other"``.

    Returns:
        The path's state, and for a regular file its bytes and permission bits.
    """
    parts = path.split("/")
    names: list[str] | None = None if twins is None else []
    try:
        parent = _walk(folder, parts[:-1], names=names)
    except OSError:
        return "other", None, None
    if parent is None:
        return None, None, None
    try:
        try:
            handle = os.open(parts[-1], os.O_RDONLY | _NO_FOLLOW | getattr(os, "O_NONBLOCK", 0), dir_fd=parent)
        except FileNotFoundError:
            return None, None, None
        except OSError:
            return "other", None, None
        with os.fdopen(handle, "rb") as body:
            status = os.fstat(body.fileno())
            if not stat.S_ISREG(status.st_mode):
                return "other", None, None
            if names is not None and twins is not None:
                # A disk that ignores case opens Foo.py for foo.py, and dir/x for Dir/x.
                found = "/".join([*names, _disk_name(parent, parts[-1])])
                if found != path:
                    return (None if found in twins else "other"), None, None
            data = body.read()
    finally:
        os.close(parent)
    blob = _Blob(hashlib.sha256(data).hexdigest(), is_executable(status.st_mode))
    return blob, data, stat.S_IMODE(status.st_mode)


def _mode_for(after: _Blob | None, current: int | None) -> int:
    """Choose a written file's permission bits: the old ones, with the version's execute bit.

    Args:
        after: The version's file, or ``None`` for a delete.
        current: The folder file's permission bits, or ``None`` for a new file.

    Returns:
        The permission bits to write.
    """
    if current is None:
        return _EXEC_MODE if after and after.executable else _FILE_MODE
    if after and after.executable:
        return current | ((current & 0o444) >> 2) if not is_executable(current) else current
    return current & ~0o111


def _put(
    folder: int,
    path: str,
    expected: _Disk,
    data: bytes | None,
    mode: int | None,
    made: list[str],
    *,
    exact: bool = False,
) -> None:
    """Write or delete one file of the user's folder, after checking it is still as expected.

    Args:
        folder: The open root folder.
        path: The ``/``-joined path below it.
        expected: The state the path must still have.
        data: The new bytes, or ``None`` to delete the file.
        mode: The new file's permission bits.
        made: The folders made so far, which a write extends.
        exact: Check only the exact path, and count a file under another case
            or Unicode form as a conflict; for a path with twins. A folder
            under another form counts as missing for a delete, and is renamed
            to the exact form, with everything in it, for a write.

    Raises:
        LocalFolderConflictError: When the path changed since the check.
        OSError: When a part on the way is a link or a file, or the write fails.
    """
    parts = path.split("/")
    parent = _walk(folder, parts[:-1], made=None if data is None else made, exact=exact)
    if parent is None:
        if expected is not None:
            raise LocalFolderConflictError([path])
        return
    try:
        if _read_disk(parent, parts[-1], set() if exact else None)[0] != expected:
            raise LocalFolderConflictError([path])
        if data is None:
            os.unlink(parts[-1], dir_fd=parent)
            return
        temporary = f".{parts[-1]}.{secrets.token_hex(6)}.apply"
        handle = os.open(temporary, os.O_WRONLY | os.O_CREAT | os.O_EXCL | _NO_FOLLOW, 0o600, dir_fd=parent)
        try:
            with os.fdopen(handle, "wb") as body:
                body.write(data)
                body.flush()
                # The umask does not apply to fchmod, so the file gets exactly these bits.
                os.fchmod(body.fileno(), mode if mode is not None else _FILE_MODE)
                os.fsync(body.fileno())
            # A rename replaces a link that appeared at the name, and never writes through it.
            os.replace(temporary, parts[-1], src_dir_fd=parent, dst_dir_fd=parent)
        except BaseException:
            with contextlib.suppress(OSError):
                os.unlink(temporary, dir_fd=parent)
            raise
    finally:
        os.close(parent)


def _put_back(folder: int, changes: list[_Change], made: list[str], *, strict: bool = False) -> bool:
    """Return changed files to their state before Apply, then remove the folders Apply made.

    Args:
        folder: The open root folder.
        changes: The changes to take back; each file is in its ``after`` state.
        made: The folders Apply made, highest first.
        strict: Raise on the first failure (Undo); else keep going (rollback of a failed Apply).

    Returns:
        Whether every file went back; folders that stay do not count.

    Raises:
        LocalFolderConflictError: With ``strict``, when a file changed since the check.
        OSError: With ``strict``, when a write fails.
    """
    restored = True
    twins = _twins(changes)
    for change in reversed(changes):
        try:
            _put(
                folder,
                change.path,
                change.after,
                change.before_data,
                change.before_mode,
                [],
                exact=change.path in twins,
            )
        except (OSError, LocalFolderConflictError):
            if strict:
                raise
            restored = False
    for path in reversed(made):
        parts = path.split("/")
        with contextlib.suppress(OSError):
            parent = _walk(folder, parts[:-1])
            if parent is not None:
                try:
                    # Only an empty folder goes; a file the user added since keeps its folder.
                    os.rmdir(parts[-1], dir_fd=parent)
                finally:
                    os.close(parent)
    return restored


def _save_record(record: Path, root: str | os.PathLike[str], changes: list[_Change], made: list[str]) -> None:
    """Write Undo's record of an Apply atomically.

    Args:
        record: Where the record goes.
        root: The run's folder, for the reader.
        changes: Every change, with the bytes and bits before Apply.
        made: The folders Apply made.
    """
    body = {
        "root": str(root),
        "made": made,
        "changes": [
            {
                "path": change.path,
                "before": _blob_json(change.before),
                "after": _blob_json(change.after),
                "data": base64.b64encode(change.before_data).decode() if change.before_data is not None else None,
                "mode": change.before_mode,
            }
            for change in changes
        ],
    }
    record.parent.mkdir(parents=True, exist_ok=True)
    descriptor, name = tempfile.mkstemp(dir=record.parent, suffix=".json")
    partial = Path(name)
    try:
        # A full disk can fail at the write, at the flush on close, or at the rename.
        with os.fdopen(descriptor, "w") as handle:
            json.dump(body, handle)
        partial.replace(record)
    except BaseException:
        partial.unlink(missing_ok=True)
        raise


def _load_record(record: Path, root: str | os.PathLike[str]) -> tuple[list[_Change], list[str]]:
    """Read Undo's record of an Apply into one folder.

    Args:
        record: The record's path.
        root: The run's folder, which must be the folder Apply wrote.

    Returns:
        The changes, with the bytes before Apply, and the folders Apply made.

    Raises:
        LocalFolderError: When there is no record, it cannot be read, or it is for another folder.
    """
    try:
        body = json.loads(record.read_text())
        recorded_root = str(body["root"])
        changes = [
            _Change(
                str(item["path"]),
                _blob_from(item["before"]),
                _blob_from(item["after"]),
                base64.b64decode(item["data"]) if item["data"] is not None else None,
                item["mode"],
            )
            for item in body["changes"]
        ]
        made = [str(path) for path in body["made"]]
    except FileNotFoundError:
        raise LocalFolderError("The best version is not in the folder, so there is nothing to undo.") from None
    except (OSError, ValueError, KeyError, TypeError):
        raise LocalFolderError("The Undo copy of this run is damaged.") from None
    if recorded_root != str(root):
        raise LocalFolderError("The Undo copy of this run is for another folder.")
    return changes, made


def _blob_json(blob: _Blob | None) -> dict[str, Any] | None:
    """Turn a file state into JSON.

    Args:
        blob: The state, or ``None`` for an absent file.

    Returns:
        ``{"sha256", "executable"}`` or ``None``.
    """
    return None if blob is None else {"sha256": blob.sha256, "executable": blob.executable}


def _blob_from(raw: Any) -> _Blob | None:
    """Read a file state from JSON.

    Args:
        raw: What :func:`_blob_json` wrote.

    Returns:
        The state, or ``None`` for an absent file.
    """
    return None if raw is None else _Blob(str(raw["sha256"]), bool(raw["executable"]))


def _members(commit: str) -> list[tarfile.TarInfo]:
    """Read the entries of a kept tree.

    Args:
        commit: The run's pinned source id.

    Returns:
        Every entry, in archive order.

    Raises:
        LocalFolderError: When the kept copy is missing or unreadable.
    """
    with _open_kept(commit) as packed:
        return packed.getmembers()


def _open_kept(commit: str) -> tarfile.TarFile:
    """Open a kept tree for reading.

    Args:
        commit: The run's pinned source id.

    Returns:
        The open archive; the caller closes it.

    Raises:
        LocalFolderError: When the kept copy is missing or unreadable.
    """
    kept = kept_snapshot(commit)
    try:
        return tarfile.open(kept, "r:gz")
    except (OSError, tarfile.TarError):
        raise LocalFolderError("The kept copy of this run's folder is missing on this computer.") from None


def _pack(root: Path, entries: list[dict[str, Any]], archive: Path) -> str:
    """Write the listed entries into a deterministic ``tree.tgz`` and hash their content.

    Owners, times, modes and the gzip header are fixed, so the same files
    give the same bytes. Each file is read only once, and the pack and the
    hash see the same bytes, so the id always matches the pack even when the
    file changes during the pack.

    Args:
        root: The folder.
        entries: Its full ``folder_entries`` listing.
        archive: The archive to write.

    Returns:
        The entries' ``folder_tree.source_id``.

    Raises:
        LocalFolderError: When the archive grows past ``MAX_ARCHIVE_BYTES``.
        OSError: When a path cannot be read, is no longer a regular file, or has a link on it since the listing.
    """
    with (
        archive.open("wb") as raw,
        gzip.GzipFile(filename="", mode="wb", fileobj=raw, mtime=0) as zipped,
        tarfile.open(fileobj=zipped, mode="w", format=tarfile.PAX_FORMAT) as packed,
    ):
        hashed: list[tuple[str, bytes]] = []
        for entry in entries:
            info = tarfile.TarInfo(entry["path"])
            if entry["type"] == "dir":
                info.type = tarfile.DIRTYPE
                info.mode = _DIR_MODE
                packed.addfile(info)
            else:
                with open_no_follow(root, entry["path"]) as body:
                    status = os.fstat(body.fileno())
                    executable = is_executable(status.st_mode)
                    info.mode = _EXEC_MODE if executable else _FILE_MODE
                    # A file that grows during the pack is cut at this size, and the hash sees the same cut.
                    info.size = status.st_size
                    digest = hashlib.sha1(usedforsecurity=False)
                    packed.addfile(info, _Tee(body, digest, raw, entries))
                    hashed.append((entry["path"], digest.digest() + (EXEC_MARKER if executable else b"")))
            if raw.tell() > MAX_ARCHIVE_BYTES:
                raise LocalFolderError(_too_large(entries))
    if archive.stat().st_size > MAX_ARCHIVE_BYTES:
        raise LocalFolderError(_too_large(entries))
    return source_id(hashed)


def _too_large(entries: list[dict[str, Any]]) -> str:
    """Explain a folder over the size cap, naming its largest top-level folders.

    Args:
        entries: The folder's ``folder_entries`` listing.

    Returns:
        The message for the run's owner.
    """
    sizes: dict[str, int] = {}
    folders = {entry["path"] for entry in entries if entry["type"] == "dir" and "/" not in entry["path"]}
    for entry in entries:
        top = entry["path"].split("/", 1)[0]
        if top in folders:
            sizes[top] = sizes.get(top, 0) + entry.get("size", 0)
    largest = sorted(sizes.items(), key=lambda item: (-item[1], item[0]))[:_LARGEST_SHOWN]
    message = f"The folder is larger than {MAX_ARCHIVE_BYTES // (1024 * 1024)} MB compressed."
    if not largest:
        return message
    named = ", ".join(f"{name}/ ({size / (1024 * 1024):.1f} MB)" for name, size in largest)
    return f"{message} The largest top-level folders are {named}. Add the ones the run does not need to .gitignore."


def _keep(archive: Path, commit: str) -> None:
    """Keep the original pack under the local state folder, keyed by its source id.

    A pack with the same id has the same bytes, so the newest one replaces an older one.

    Args:
        archive: The packed tree, before any draft is written over it.
        commit: Its SHA-1.

    Raises:
        LocalFolderError: When this computer has no local state folder.
    """
    kept = kept_snapshot(commit)
    kept.parent.mkdir(parents=True, exist_ok=True)
    with tempfile.NamedTemporaryFile(dir=kept.parent, suffix=".tgz", delete=False) as handle:
        partial = Path(handle.name)
    try:
        shutil.copyfile(archive, partial)
        # A reader never sees half a copy: the rename is atomic on one disk.
        partial.replace(kept)
    except BaseException:
        partial.unlink(missing_ok=True)
        raise


__all__ = [
    "APPLIED_FOLDER",
    "APPLY_LOCK",
    "FRESH_PACK_SECONDS",
    "KEPT_FOLDER",
    "LOCAL_PROVIDER",
    "LOCAL_SANDBOX_PROVIDER",
    "LocalFolderConflictError",
    "LocalFolderError",
    "applied_record",
    "apply_version",
    "ensure_runs_here",
    "forget_snapshot",
    "is_applied",
    "is_local",
    "kept_snapshot",
    "snapshot_file",
    "snapshot_folder",
    "snapshot_tree",
    "snapshot_zip",
    "undo_version",
]
