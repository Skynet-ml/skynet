"""Write a repository run's draft files over its tree, in the trusted parent.

The setup agent and the user change repository files in the setup draft. The
run request carries them as ``target.repo.draft_files``. Every place that
rebuilds the run's tree from its pinned commit writes them over it, so the
run, the served version, the download and the published branch all start
from the same tree. The user's repository stays unchanged until a publish.
"""

from __future__ import annotations

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


def apply_to_archive(
    archive: Path,
    drafts: Mapping[str, str] | None,
    readonly_paths: Iterable[str] = (),
    max_bytes: int = MAX_ARCHIVE_BYTES,
) -> None:
    """Write the draft files over a packed tree, in place.

    A draft replaces the text of an existing regular file and keeps its mode,
    or adds a new file. It never follows a link: a draft whose path, or a
    folder above it, is a link or a file in the tree is refused.

    Args:
        archive: The ``tree.tgz`` that :func:`~.github_repo.fetch_snapshot` packed.
        drafts: Repository path to file text; ``None`` or empty leaves the archive as it is.
        readonly_paths: Submodule and Git LFS paths, which a draft cannot change.
        max_bytes: The compressed size the patched tree must stay within.

    Raises:
        DraftFileError: When a draft path is invalid, read-only, or meets a
            link, a folder or a file where it needs the other kind, or the
            patched tree is larger than ``max_bytes``.
    """
    files = clean_drafts(drafts)
    if not files:
        return
    readonly = tuple(readonly_paths)
    with tarfile.open(archive) as source:
        members = {posixpath.normpath(member.name): member for member in source.getmembers()}
    # The packed tree need not list a folder on its own, so its files name it.
    folders = {"/".join(name.split("/")[:depth]) for name in members for depth in range(1, name.count("/") + 1)}
    for path in files:
        if path_inside(path, readonly):
            raise DraftFileError(f"{path} is read-only in this repository, so the run cannot change it.")
        parts = path.split("/")
        for depth in range(1, len(parts)):
            parent = members.get("/".join(parts[:depth]))
            if parent is not None and not parent.isdir():
                raise DraftFileError(f"{path} lies under a link or a file in the repository, so it cannot be written.")
        existing = members.get(path)
        if path in folders or (existing is not None and not existing.isreg()):
            raise DraftFileError(f"{path} is a link or a folder in the repository, so it cannot be written.")
    with tempfile.NamedTemporaryFile(dir=archive.parent, suffix=".tgz", delete=False) as handle:
        patched = Path(handle.name)
    try:
        with tarfile.open(archive) as source, tarfile.open(patched, "w:gz") as target:
            for member in source:
                name = posixpath.normpath(member.name)
                if name in files:
                    continue
                target.addfile(member, source.extractfile(member) if member.isreg() else None)
            for path, text in files.items():
                body = text.encode("utf-8")
                existing = members.get(path)
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


def write_to_checkout(
    checkout: Path, drafts: Mapping[str, str] | None, readonly_paths: Iterable[str] = ()
) -> list[str]:
    """Write the draft files into a git checkout of the pinned commit.

    Args:
        checkout: The working tree.
        drafts: Repository path to file text; ``None`` or empty writes nothing.
        readonly_paths: Submodule and Git LFS paths, which a draft cannot change.

    Returns:
        The clean paths written, for ``git add``.

    Raises:
        DraftFileError: When a draft path is invalid or read-only, or a path or
            a folder above it is a link, or a file stands where a folder must be.
    """
    files = clean_drafts(drafts)
    readonly = tuple(readonly_paths)
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


__all__ = ["MAX_ARCHIVE_BYTES", "DraftFileError", "apply_to_archive", "clean_drafts", "write_to_checkout"]
