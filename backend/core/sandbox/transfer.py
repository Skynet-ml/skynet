"""Move many files into or out of a sandbox as one tar archive, with safe paths.

Adapted from Harbor (harbor-framework/harbor), Apache-2.0: the tar upload
and download with the ``data`` extraction filter, the default exclusion list
and the ``path_safety`` member check. Changes: the Harbor 0.25.0 source was
not available when this was ported, so the exclusion names follow Harbor's
documented categories (version control, caches, virtual envs); the API is two
plain functions over bytes; and an unsafe member raises before any file is
written instead of being skipped.
"""

from __future__ import annotations

import fnmatch
import io
import os
import tarfile
from collections.abc import Iterable
from pathlib import Path

DEFAULT_EXCLUDE: frozenset[str] = frozenset(
    {
        ".git",
        ".hg",
        ".svn",
        "__pycache__",
        ".mypy_cache",
        ".pytest_cache",
        ".ruff_cache",
        ".ipynb_checkpoints",
        ".tox",
        ".nox",
        ".venv",
        "venv",
        "node_modules",
        ".DS_Store",
    }
)


class UnsafeArchiveError(ValueError):
    """Raised when an archive member would land outside the destination or is a device file."""


def pack(paths: Iterable[str | os.PathLike[str]], exclude: Iterable[str] = ()) -> bytes:
    """Pack files and directories into an uncompressed tar archive.

    Each path goes into the archive under its own base name; directories are
    added recursively. A member is left out when any part of its archive path
    matches a name or glob in ``DEFAULT_EXCLUDE`` or ``exclude``.

    Args:
        paths: Files or directories to pack.
        exclude: Extra names or glob patterns to leave out.

    Returns:
        The tar archive bytes.
    """
    patterns = DEFAULT_EXCLUDE | set(exclude)

    def skip_excluded(info: tarfile.TarInfo) -> tarfile.TarInfo | None:
        """Drop a member when any part of its archive path matches an exclusion.

        Args:
            info: The member that ``tarfile`` is about to add.

        Returns:
            The member, or ``None`` to leave it (and its children) out.
        """
        parts = Path(info.name).parts
        if any(fnmatch.fnmatchcase(part, pattern) for part in parts for pattern in patterns):
            return None
        return info

    buffer = io.BytesIO()
    with tarfile.open(fileobj=buffer, mode="w") as tar:
        for path in paths:
            source = Path(path)
            tar.add(source, arcname=source.name, filter=skip_excluded)
    return buffer.getvalue()


def unpack(data: bytes, dest: str | os.PathLike[str]) -> None:
    """Extract a tar archive into ``dest``, refusing any unsafe member.

    Every member is checked before anything is written, so a plain unsafe
    archive leaves ``dest`` untouched. A symlink chain that only escapes once
    earlier links exist on disk is caught during extraction, after earlier
    members are written.

    Args:
        data: Tar archive bytes, plain or compressed.
        dest: Directory to extract into; it is created when missing.

    Raises:
        UnsafeArchiveError: A member has an absolute path, resolves outside
            ``dest``, links outside ``dest``, or is a device file or FIFO.
    """
    root = Path(dest)
    root.mkdir(parents=True, exist_ok=True)
    root = root.resolve()
    with tarfile.open(fileobj=io.BytesIO(data), mode="r:*") as tar:
        members = tar.getmembers()
        for member in members:
            _check_member(member, root)
        if hasattr(tarfile, "data_filter"):
            try:
                tar.extractall(root, filter="data")
            except tarfile.FilterError as exc:
                raise UnsafeArchiveError(str(exc)) from exc
            return
        # Python 3.11.0-3.11.3 has no extraction filters. Re-check each member
        # right before it is written, because links extracted earlier change
        # what later paths resolve to.
        for member in members:
            _check_member(member, root)
            tar.extract(member, root)


def _check_member(member: tarfile.TarInfo, root: Path) -> None:
    """Refuse a member whose path or link target resolves outside ``root``.

    Args:
        member: The archive member to check.
        root: The resolved destination directory.

    Raises:
        UnsafeArchiveError: The member is unsafe to extract.
    """
    name = member.name
    if Path(name).is_absolute() or name.startswith(("/", "\\")):
        raise UnsafeArchiveError(f"absolute path in archive: {name!r}")
    if member.isdev():
        raise UnsafeArchiveError(f"device file or FIFO in archive: {name!r}")
    target = root / name
    _require_inside(target, root, name)
    if member.issym():
        if Path(member.linkname).is_absolute():
            raise UnsafeArchiveError(f"absolute symlink target in archive: {name!r}")
        _require_inside(target.parent / member.linkname, root, name)
    elif member.islnk():
        _require_inside(root / member.linkname, root, name)


def _require_inside(path: Path, root: Path, name: str) -> None:
    """Raise unless ``path`` resolves to ``root`` or a path under it.

    Args:
        path: The path to resolve, following symlinks already on disk.
        root: The resolved destination directory.
        name: The member name, for the error message.

    Raises:
        UnsafeArchiveError: ``path`` resolves outside ``root``.
    """
    resolved = os.path.realpath(path)
    if os.path.commonpath([str(root), resolved]) != str(root):
        raise UnsafeArchiveError(f"archive member resolves outside the destination: {name!r}")
