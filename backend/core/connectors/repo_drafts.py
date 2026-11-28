"""Write a repository run's draft files over its tree, in the trusted parent.

The setup agent and the user change repository files in the setup draft. The
run request carries them as ``target.repo.draft_files``. Every place that
rebuilds the run's tree from its pinned commit writes them over it, so the
run, the served version, the download and the published branch all start
from the same tree. The user's repository stays unchanged until a publish.
A Mac app run can also carry ``draft_deleted``: the folder files its setup
deleted, which the same places remove (SKY-997).
"""

from __future__ import annotations

import contextlib
import io
import posixpath
import tarfile
import tempfile
from collections.abc import Iterable, Mapping
from pathlib import Path

from ..models.blackbox import clean_draft_path, path_inside

# Compressed tree ceiling: the tree crosses into each sandbox as base64 text.
# It lives here, not in ``github_repo``, as that module imports this one.
MAX_ARCHIVE_BYTES = 200 * 1024 * 1024


class DraftFileError(ValueError):
    """A draft file cannot be written over the repository's tree."""


def clean_drafts(drafts: Mapping[str, str] | None) -> dict[str, str]:
    """Check every draft path again, as a stored payload can be older than the request rule.

    Args:
        drafts: Repository path to file text; ``None`` for none.

    Returns:
        The drafts under their clean paths.

    Raises:
        DraftFileError: When a path fails :func:`~core.models.blackbox.clean_draft_path`,
            or one draft file lies under another.
    """
    cleaned: dict[str, str] = {}
    for raw, text in (drafts or {}).items():
        try:
            cleaned[clean_draft_path(str(raw))] = str(text)
        except ValueError as error:
            raise DraftFileError(str(error)) from None
    for path in cleaned:
        if path_inside(path, [other for other in cleaned if other != path]):
            raise DraftFileError(f"{path} lies under another draft file, so it cannot be written.")
    return cleaned


def clean_deleted(deleted: Iterable[str], files: Mapping[str, str]) -> list[str]:
    """Check every deleted draft path again, as a stored payload can be older than the request rule.

    Args:
        deleted: The paths the draft deletes.
        files: The clean draft files.

    Returns:
        The clean paths, each once.

    Raises:
        DraftFileError: When a path fails :func:`~core.models.blackbox.clean_draft_path`,
            or a draft file has the same path.
    """
    cleaned: list[str] = []
    for raw in deleted:
        try:
            path = clean_draft_path(str(raw))
        except ValueError as error:
            raise DraftFileError(str(error)) from None
        # A nested pair is a file swapped with a folder; the deletes go first, so it is no conflict.
        if path in files:
            raise DraftFileError(f"{path} is both written and deleted, so it cannot be changed.")
        if path not in cleaned:
            cleaned.append(path)
    return cleaned


def apply_to_archive(
    archive: Path,
    drafts: Mapping[str, str] | None,
    readonly_paths: Iterable[str] = (),
    max_bytes: int = MAX_ARCHIVE_BYTES,
    deleted: Iterable[str] = (),
) -> None:
    """Write the draft files over a packed tree, and remove the deleted ones, in place.

    A draft replaces the text of an existing regular file and keeps its mode,
    or adds a new file. It never follows a link: a draft whose path, or a
    folder above it, is a link or a file in the tree is refused. A deleted
    path that the tree does not hold is no error. The deletes apply first, so
    a draft can replace a deleted file with a folder, or a folder that the
    deletes empty with a file.

    Args:
        archive: The ``tree.tgz`` that :func:`~.github_repo.fetch_snapshot` packed.
        drafts: Repository path to file text; ``None`` or empty leaves the archive as it is.
        readonly_paths: Submodule and Git LFS paths, which a draft cannot change.
        max_bytes: The compressed size the patched tree must stay within.
        deleted: The paths the draft deletes.

    Raises:
        DraftFileError: When a draft path is invalid, read-only, or meets a
            link, a folder or a file where it needs the other kind, when a
            deleted path is read-only or not a regular file, or the patched
            tree is larger than ``max_bytes``.
    """
    files = clean_drafts(drafts)
    gone = clean_deleted(deleted, files)
    if not files and not gone:
        return
    readonly = tuple(readonly_paths)
    with tarfile.open(archive) as source:
        members = {posixpath.normpath(member.name): member for member in source.getmembers()}
    for path in gone:
        existing = members.get(path)
        if path_inside(path, readonly) or (existing is not None and not existing.isreg()):
            raise DraftFileError(f"{path} is read-only, a link or a folder in the repository, so it cannot be deleted.")
    kept = _kept_members(members, gone)
    # The packed tree need not list a folder on its own, so its files name it.
    folders = {"/".join(name.split("/")[:depth]) for name in kept for depth in range(1, name.count("/") + 1)}
    for path in files:
        if path_inside(path, readonly):
            raise DraftFileError(f"{path} is read-only in this repository, so the run cannot change it.")
        parts = path.split("/")
        for depth in range(1, len(parts)):
            parent = kept.get("/".join(parts[:depth]))
            if parent is not None and not parent.isdir():
                raise DraftFileError(f"{path} lies under a link or a file in the repository, so it cannot be written.")
        existing = kept.get(path)
        if path in folders or (existing is not None and not existing.isreg()):
            raise DraftFileError(f"{path} is a link or a folder in the repository, so it cannot be written.")
    with tempfile.NamedTemporaryFile(dir=archive.parent, suffix=".tgz", delete=False) as handle:
        patched = Path(handle.name)
    try:
        with tarfile.open(archive) as source, tarfile.open(patched, "w:gz") as target:
            for member in source:
                name = posixpath.normpath(member.name)
                if name in files or name not in kept:
                    continue
                target.addfile(member, source.extractfile(member) if member.isreg() else None)
            for path, text in files.items():
                body = text.encode("utf-8")
                existing = kept.get(path)
                member = tarfile.TarInfo(path)
                member.mode = existing.mode if existing is not None else 0o644
                member.mtime = existing.mtime if existing is not None else 0
                member.size = len(body)
                target.addfile(member, io.BytesIO(body))
        # The fetch checked the size before the drafts; they can push the tree over it.
        if patched.stat().st_size > max_bytes:
            raise DraftFileError(
                f"The repository with the draft files is larger than {max_bytes // (1024 * 1024)} MB compressed."
            )
        patched.replace(archive)
    except BaseException:
        patched.unlink(missing_ok=True)
        raise


def _kept_members(members: Mapping[str, tarfile.TarInfo], gone: Iterable[str]) -> dict[str, tarfile.TarInfo]:
    """Leave out the deleted members, and the folder members that the deletes empty.

    Args:
        members: The packed tree's members by clean name.
        gone: The deleted paths.

    Returns:
        The members that stay. A folder that was empty before the deletes stays.
    """
    gone = set(gone)
    kept = {name: member for name, member in members.items() if name not in gone}
    emptied = {posixpath.dirname(path) for path in gone} - {""}
    while emptied:
        name = emptied.pop()
        member = kept.get(name)
        if member is not None and member.isdir() and not any(other.startswith(f"{name}/") for other in kept):
            del kept[name]
            if parent := posixpath.dirname(name):
                emptied.add(parent)
    return kept


def write_to_checkout(
    checkout: Path,
    drafts: Mapping[str, str] | None,
    readonly_paths: Iterable[str] = (),
    deleted: Iterable[str] = (),
) -> list[str]:
    """Write the draft files into a git checkout of the pinned commit, and remove the deleted ones.

    Args:
        checkout: The working tree.
        drafts: Repository path to file text; ``None`` or empty writes nothing.
        readonly_paths: Submodule and Git LFS paths, which a draft cannot change.
        deleted: The paths the draft deletes; a missing one is no error.

    Returns:
        The clean paths written, for ``git add``.

    Raises:
        DraftFileError: When a draft path is invalid or read-only, or a path or
            a folder above it is a link, or a file stands where a folder must be,
            or a deleted path is read-only or not a regular file. The deletes
            apply first, so a draft can take the path of a deleted file or of a
            folder that the deletes empty.
    """
    files = clean_drafts(drafts)
    gone = clean_deleted(deleted, files)
    readonly = tuple(readonly_paths)
    for path in gone:
        parts = path.split("/")
        target = checkout.joinpath(*parts)
        linked = any(checkout.joinpath(*parts[:depth]).is_symlink() for depth in range(1, len(parts) + 1))
        if path_inside(path, readonly) or linked or target.is_dir():
            raise DraftFileError(f"{path} is read-only, a link or a folder in the repository, so it cannot be deleted.")
    for path in gone:
        # A file above the path means the path is missing too.
        with contextlib.suppress(FileNotFoundError, NotADirectoryError):
            checkout.joinpath(*path.split("/")).unlink()
        # Git keeps no empty folder, so a folder the deletes empty goes, and a draft file can take its path.
        parent = checkout.joinpath(*path.split("/")).parent
        while parent != checkout:
            try:
                parent.rmdir()
            except OSError:
                break
            parent = parent.parent
    for path in files:
        if path_inside(path, readonly):
            raise DraftFileError(f"{path} is read-only in this repository, so the run cannot change it.")
        parts = path.split("/")
        for depth in range(1, len(parts)):
            parent = checkout.joinpath(*parts[:depth])
            if parent.is_symlink() or (parent.exists() and not parent.is_dir()):
                raise DraftFileError(f"{path} lies under a link or a file in the repository, so it cannot be written.")
        target = checkout.joinpath(*parts)
        if target.is_symlink() or target.is_dir():
            raise DraftFileError(f"{path} is a link or a folder in the repository, so it cannot be written.")
    for path, text in files.items():
        target = checkout.joinpath(*path.split("/"))
        target.parent.mkdir(parents=True, exist_ok=True)
        target.write_bytes(text.encode("utf-8"))
    return list(files)


__all__ = [
    "MAX_ARCHIVE_BYTES",
    "DraftFileError",
    "apply_to_archive",
    "clean_deleted",
    "clean_drafts",
    "write_to_checkout",
]
