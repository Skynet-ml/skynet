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

import copy
import errno
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

# Python 3.11.0-3.11.3 has no FilterError; an empty tuple catches nothing.
_FILTER_ERRORS: tuple[type[Exception], ...] = (tarfile.FilterError,) if hasattr(tarfile, "FilterError") else ()

# A link chain past PATH_MAX fails with these on patched Pythons; other OS errors,
# such as a full disk, are not about the archive and propagate unchanged.
_UNSAFE_ERRNOS = frozenset({errno.ENAMETOOLONG, errno.ELOOP})


class UnsafeArchiveError(ValueError):
    """Raised when an archive member would land outside the destination or is a device file."""


def pack(paths: Iterable[str | os.PathLike[str]], exclude: Iterable[str] = ()) -> bytes:
    """Pack files and directories into an uncompressed tar archive.

    Each path goes into the archive under its own base name; directories are
    added recursively. A path such as ``.`` or ``sub/..`` takes the name of
    the directory it resolves to. A member is left out when any part of its archive path
    matches a name or glob in ``DEFAULT_EXCLUDE`` or ``exclude``. Patterns match
    one path part at a time, so a pattern with a ``/`` (such as ``src/venv``)
    never matches; use the bare name instead.

    Args:
        paths: Files or directories to pack.
        exclude: Extra names or glob patterns to leave out.

    Returns:
        The tar archive bytes.

    Raises:
        ValueError: A path has no base name, such as the filesystem root.
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
            # ``..`` and ``.`` have no usable lexical name; a symlink keeps its own name.
            name = source.name if source.name not in ("", "..") else source.resolve().name
            if not name:
                raise ValueError(f"path has no base name to pack: {str(path)!r}")
            tar.add(source, arcname=name, filter=skip_excluded)
    return buffer.getvalue()


def unpack(data: bytes, dest: str | os.PathLike[str]) -> None:
    """Extract a tar archive into ``dest``, refusing any unsafe member.

    Every member is checked before anything is written, so an unsafe archive
    leaves ``dest`` untouched. A member whose path or link target goes through
    a link in the same archive is refused, because only the lexical check is
    safe against symlink chains longer than ``PATH_MAX``. A hardlink must name
    an earlier regular file in the same archive.

    Only uncompressed tar is accepted, as ``pack`` writes it, so the bytes
    written are bounded by the size of ``data``.

    Args:
        data: Uncompressed tar archive bytes.
        dest: Directory to extract into; it is created when missing.

    Raises:
        UnsafeArchiveError: A member has an absolute path, resolves outside
            ``dest``, links outside ``dest``, goes through a link in the
            archive, hardlinks to anything but an earlier regular file, or is
            a device file or FIFO.
        tarfile.ReadError: ``data`` is not an uncompressed tar archive.
        OSError: Writing to ``dest`` fails for a reason unrelated to the archive.
    """
    root = Path(dest)
    root.mkdir(parents=True, exist_ok=True)
    root = root.resolve()
    with tarfile.open(fileobj=io.BytesIO(data), mode="r:") as tar:
        members = tar.getmembers()
        # A hardlink to a symlink member extracts as a second symlink, so both count.
        links = {os.path.normpath(m.name) for m in members if m.issym() or m.islnk()}
        files: set[str] = set()
        for member in members:
            _check_member(member, root)
            _check_no_link_parent(member, links)
            _check_hardlink_target(member, files, links)
            if member.isreg():
                files.add(os.path.normpath(member.name))
        try:
            if hasattr(tarfile, "data_filter"):
                tar.extractall(root, filter="data")
            else:
                _extract_without_filter(tar, members, root)
        except _FILTER_ERRORS as exc:
            raise UnsafeArchiveError(str(exc)) from exc
        except OSError as exc:
            if exc.errno not in _UNSAFE_ERRNOS:
                raise
            raise UnsafeArchiveError(str(exc)) from exc


def _extract_without_filter(tar: tarfile.TarFile, members: list[tarfile.TarInfo], root: Path) -> None:
    """Extract members one by one on Python 3.11.0-3.11.3, which has no extraction filters.

    Each member is re-checked right before it is written, because links
    extracted earlier change what later paths resolve to.

    Args:
        tar: The open archive.
        members: The archive members, already checked.
        root: The resolved destination directory.

    Raises:
        UnsafeArchiveError: A member resolves outside ``root`` at write time.
    """
    for member in members:
        _check_member(member, root)
        if member.isreg():
            tar.extract(_safe_file(member), root)
        else:
            # Directory modes are left at the default so a read-only
            # directory cannot block the members written into it later.
            tar.extract(member, root, set_attrs=False)


def _safe_file(member: tarfile.TarInfo) -> tarfile.TarInfo:
    """Copy a regular file member with the modes and owner that the ``data`` filter allows.

    Args:
        member: The regular file member.

    Returns:
        A copy without setuid, setgid, sticky or group/other write bits, that
        the owner can read and write, and with no owner change on extraction.
    """
    safe = copy.copy(member)
    mode = member.mode & 0o755
    if not mode & 0o100:
        mode &= ~0o011
    safe.mode = mode | 0o600
    safe.uid = safe.gid = -1
    safe.uname = safe.gname = ""
    return safe


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


def _check_hardlink_target(member: tarfile.TarInfo, files: set[str], links: set[str]) -> None:
    """Refuse a hardlink unless it names an earlier regular file that no link replaces.

    Extraction looks up a hardlink target among the earlier members only, so a
    missing target fails after earlier files are written. Checking here keeps
    a refused archive from writing anything.

    Args:
        member: The archive member to check.
        files: Normalized names of the regular file members before ``member``.
        links: Normalized names of every symlink and hardlink member.

    Raises:
        UnsafeArchiveError: The hardlink target is not an earlier regular file.
    """
    if not member.islnk():
        return
    target = os.path.normpath(member.linkname)
    if target not in files or target in links:
        raise UnsafeArchiveError(f"hardlink target is not an earlier regular file in archive: {member.name!r}")


def _check_no_link_parent(member: tarfile.TarInfo, links: set[str]) -> None:
    """Refuse a member whose path or link target passes through a link in the archive.

    Each prefix of the raw path is checked, so ``here/../x`` is refused when
    ``here`` is a link even though it normalizes to ``x``.

    Args:
        member: The archive member to check.
        links: Normalized names of every symlink and hardlink member.

    Raises:
        UnsafeArchiveError: A path goes through an archive link or above the destination.
    """
    paths = [member.name]
    if member.issym():
        paths.append(str(Path(member.name).parent / member.linkname))
    elif member.islnk():
        paths.append(member.linkname)
    for path in paths:
        if Path(path).is_absolute():
            raise UnsafeArchiveError(f"absolute link target in archive: {member.name!r}")
        parts = Path(path).parts
        prefixes = [os.path.normpath(Path(*parts[: i + 1])) for i in range(len(parts))]
        if any(p == ".." or p.startswith("../") for p in prefixes):
            raise UnsafeArchiveError(f"archive member goes above the destination: {member.name!r}")
        if any(p in links for p in prefixes[:-1]):
            raise UnsafeArchiveError(f"archive member goes through a link: {member.name!r}")


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
