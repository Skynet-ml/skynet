"""Pack a folder on this computer as a repository run's source, with no git (SKY-990).

The desktop local stack can run on a folder instead of a hosted repository.
The folder is packed into the same ``tree.tgz`` a git snapshot ships, so the
rest of staging (drafts, hidden-path strip, gates) runs unchanged. A content
hash of the kept files stands in for the commit id (``folder_tree.folder_source_id``),
and a copy is kept under ``settings.local_state_dir`` for the run-detail file
browser and for Apply/Undo. Nothing here writes the folder, and nothing leaves this
computer: only the local ``docker`` provider may run a local source.
"""

from __future__ import annotations

import gzip
import hashlib
import os
import re
import shutil
import stat
import tarfile
import tempfile
import zipfile
from collections.abc import Iterable, Mapping
from pathlib import Path
from typing import Any

from ..config import settings
from ..connectors.github_repo import RepoFetchError, RepoSnapshot
from ..connectors.repo_drafts import MAX_ARCHIVE_BYTES, DraftFileError, write_to_checkout
from ..local.folder_tree import FolderTreeError, folder_entries, open_no_follow, source_id
from ..models.blackbox import LOCAL_FOLDER_ONLY_HERE, LOCAL_REPO_PROVIDER, REPO_COMMIT_PATTERN
from ..service_gateway.optimization.blackbox.repo_tree import apply_patch

LOCAL_PROVIDER = LOCAL_REPO_PROVIDER
LOCAL_SANDBOX_PROVIDER = "docker"
KEPT_FOLDER = "local-folders"
_LARGEST_SHOWN = 5


class LocalFolderError(RepoFetchError):
    """Explain why a local folder cannot be packed or read as a run's source."""


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
        if kept.is_file():
            shutil.copyfile(kept, archive)
            return RepoSnapshot(commit=commit, archive=archive, readonly_paths=(), size_bytes=archive.stat().st_size)
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
) -> Path:
    """Zip a local run's kept tree with the drafts written and one version's patch applied.

    Args:
        commit: The run's pinned source id.
        patch: A unified diff against the kept tree; empty for the tree as is.
        workdir: Empty folder the checkout and zip go in.
        folder: The one top-level folder name inside the zip.
        drafts: The run's ``draft_files``, which the patch was made against.
        readonly_paths: Paths a draft cannot change.

    Returns:
        The zip, its entries under ``<folder>/``, like a host run's zip.

    Raises:
        LocalFolderError: When the kept copy is missing, a draft cannot be
            written, the patch does not apply, or the zip is larger than the limit.
    """
    checkout = workdir / "checkout"
    checkout.mkdir(parents=True)
    with _open_kept(commit) as packed:
        packed.extractall(checkout, filter="data")
    try:
        write_to_checkout(checkout, drafts, readonly_paths)
        apply_patch(checkout, patch)
    except (DraftFileError, RuntimeError) as error:
        raise LocalFolderError(str(error)) from None
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

    Owners, times and the gzip header are fixed, so the same files give the
    same bytes. Each file is hashed from the same open handle it is packed
    from, so the id always matches the packed bytes.

    Args:
        root: The folder.
        entries: Its full ``folder_entries`` listing.
        archive: The archive to write.

    Returns:
        The entries' ``folder_tree.source_id``.

    Raises:
        LocalFolderError: When the archive grows past ``MAX_ARCHIVE_BYTES``.
        OSError: When a path cannot be read, or turned into a link since the listing.
    """
    with (
        archive.open("wb") as raw,
        gzip.GzipFile(filename="", mode="wb", fileobj=raw, mtime=0) as zipped,
        tarfile.open(fileobj=zipped, mode="w", format=tarfile.PAX_FORMAT) as packed,
    ):
        hashed: list[tuple[str, bytes]] = []
        for entry in entries:
            path = root / entry["path"]
            info = tarfile.TarInfo(entry["path"])
            if entry["type"] == "dir":
                info.type = tarfile.DIRTYPE
                info.mode = stat.S_IMODE(os.lstat(path).st_mode)
                packed.addfile(info)
            else:
                with open_no_follow(path) as body:
                    status = os.fstat(body.fileno())
                    info.mode = stat.S_IMODE(status.st_mode)
                    info.size = status.st_size
                    hashed.append((entry["path"], hashlib.file_digest(body, "sha1").digest()))
                    body.seek(0)
                    packed.addfile(info, body)
            # Stopping early spares packing a huge folder only to refuse it.
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

    The newest pack replaces an older one with the same id, because the id
    leaves out file modes that the pack keeps.

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
    "KEPT_FOLDER",
    "LOCAL_PROVIDER",
    "LOCAL_SANDBOX_PROVIDER",
    "LocalFolderError",
    "ensure_runs_here",
    "is_local",
    "kept_snapshot",
    "snapshot_file",
    "snapshot_folder",
    "snapshot_tree",
    "snapshot_zip",
]
