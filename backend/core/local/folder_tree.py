"""List a folder on this computer and hash it into a source id (SKY-990, SKY-995).

The run packer (``core.worker.local_folder``) and the setup agent's folder
browser both read a local folder through this module, so they list the same
files and compute the same id: the id the setup agent records as a draft's
base is the id the run pins as its commit. This module is a leaf: it imports
nothing from the worker or the service gateway.

The listing follows the folder's ``.gitignore`` files with git's rules and
always leaves out git data, installed packages, Python virtual environments,
``.env*`` names (which can hold secrets) and every symbolic link.
"""

from __future__ import annotations

import errno
import hashlib
import io
import os
import re
import stat
import threading
from collections.abc import Iterable
from dataclasses import dataclass
from pathlib import Path
from typing import Any

# Always left out, with or without a .gitignore: tool state and installed
# packages that a run never needs and that can be very large.
SKIPPED_DIRS = frozenset({".git", "node_modules", ".venv", "venv", "__pycache__"})
# Files and folders that the operating system writes on its own (Finder,
# Explorer, Spotlight). They stay out so that opening a folder does not move its id.
OS_NAMES = frozenset(
    {
        ".ds_store",
        ".spotlight-v100",
        ".trashes",
        ".fseventsd",
        ".temporaryitems",
        "thumbs.db",
        "ehthumbs.db",
        "desktop.ini",
    }
)
# A folder holding this file is a Python virtual environment, whatever its name.
VENV_MARKER = "pyvenv.cfg"
GITIGNORE = ".gitignore"
FOLDER_ENTRIES_MAX = 20_000
# The digest stands in for a file that went away or cannot be read while the id is computed.
UNREADABLE_DIGEST = b"\0unreadable"
# Follows a file's digest in the id when the file has an execute bit, because the pack keeps that bit.
EXEC_MARKER = b"x"
_DIGEST_CACHE_MAX = 100_000

_digests: dict[tuple[str, int, int, int, int], bytes] = {}
_digests_lock = threading.Lock()


class FolderTreeError(ValueError):
    """Explain why a folder cannot be listed."""


@dataclass(frozen=True)
class _Rule:
    """One ``.gitignore`` line, compiled."""

    regex: re.Pattern[str]
    negated: bool
    dir_only: bool


def is_env_name(name: str) -> bool:
    """Check whether a file or folder name is a ``.env*`` name, which can hold secrets.

    macOS disks ignore case, so ``.Env`` names the same file as ``.env``.

    Args:
        name: One path part.

    Returns:
        True for ``.env``, ``.env.local``, ``.envrc``, ``.ENV`` and the like.
    """
    return name.lower().startswith(".env")


def is_os_name(name: str) -> bool:
    """Check whether a file or folder name is one that the operating system writes on its own.

    Args:
        name: One path part.

    Returns:
        True for :data:`OS_NAMES` in any case, and for ``._*`` AppleDouble files.
    """
    return name.startswith("._") or name.lower() in OS_NAMES


def is_executable(mode: int) -> bool:
    """Check whether a file mode has any execute bit.

    Args:
        mode: The file's ``st_mode``.

    Returns:
        True when the owner, group or others may run the file.
    """
    return bool(mode & 0o111)


def folder_entries(
    root: str | os.PathLike[str], limit: int | None = FOLDER_ENTRIES_MAX
) -> tuple[list[dict[str, Any]], bool]:
    """List a folder's kept folders and files, in the shape of a host's tree listing.

    The ``.gitignore`` files apply with git's rules: the root file and nested
    ones, the last matching line wins, a deeper file wins over a higher one,
    and an ignored folder is never entered. :data:`SKIPPED_DIRS`, folders
    with a :data:`VENV_MARKER`, ``.env*`` names, operating system files
    (:func:`is_os_name`) and every symbolic link are
    left out on top. Sockets, pipes and devices are left out too. A subfolder
    that cannot be read is listed with no children.

    Args:
        root: The folder.
        limit: Stop after this many entries; ``None`` lists every entry.

    Returns:
        ``[{"path", "type": "dir"} | {"path", "type": "file", "size"}]`` in
        walk order (each folder's children sorted by name), and whether the
        list stopped at ``limit``.

    Raises:
        FolderTreeError: When ``root`` is not a folder.
    """
    base = Path(root)
    if base.is_symlink() or not base.is_dir():
        raise FolderTreeError(f"The folder {base} does not exist or is not a folder.")
    entries: list[dict[str, Any]] = []
    complete = _visit(base, "", [], entries, limit)
    return entries, not complete


def folder_source_id(root: str | os.PathLike[str], entries: list[dict[str, Any]] | None = None) -> str:
    """Hash a folder's listed files into a 40-hex id, which fits a commit or ``draft_base``.

    Args:
        root: The folder.
        entries: A full :func:`folder_entries` listing of ``root``, listed
            again with no limit when ``None``. A listing cut at a limit gives
            another id than the run's.

    Returns:
        The :func:`source_id` of each listed file's path and :func:`file_digest`.

    Raises:
        FolderTreeError: When ``root`` is not a folder.
    """
    base = Path(root)
    listed = folder_entries(base, limit=None)[0] if entries is None else entries
    files = []
    for entry in listed:
        if entry.get("type") != "file":
            continue
        try:
            digest = file_digest(base, str(entry["path"]))
        except OSError:
            # A file that changed or went away since the listing still moves the id.
            digest = UNREADABLE_DIGEST
        files.append((str(entry["path"]), digest))
    return source_id(files)


def source_id(files: Iterable[tuple[str, bytes]]) -> str:
    """Hash file paths and content digests into a 40-hex source id.

    Args:
        files: Each file's ``/``-joined path below the root and its content digest.

    Returns:
        The SHA-1 over each path, a NUL byte and its digest, in path order.
    """
    digest = hashlib.sha1(usedforsecurity=False)
    for path, content in sorted(files):
        digest.update(path.encode("utf-8", "surrogateescape") + b"\0")
        digest.update(content)
    return digest.hexdigest()


def file_digest(root: Path, rel: str) -> bytes:
    """Hash one file's content and execute bit, reusing the digest while the file's stat stays the same.

    The setup agent asks for the id on every turn, so a big file is hashed
    only once until it changes.

    Args:
        root: The folder.
        rel: The file's ``/``-joined path below ``root``; no link on the way is followed.

    Returns:
        The 20-byte SHA-1 digest of the content, then :data:`EXEC_MARKER`
        when the file has an execute bit.

    Raises:
        OSError: When the file cannot be opened or read, is not a regular
            file, or a link is on its path below ``root``.
    """
    with open_no_follow(root, rel) as body:
        info = os.fstat(body.fileno())
        # A chmod, or a rewrite that restores the old time, changes only the ctime.
        key = (str(root / rel), info.st_size, info.st_mtime_ns, info.st_ino, info.st_ctime_ns)
        with _digests_lock:
            cached = _digests.get(key)
        if cached is not None:
            return cached
        digest = hashlib.file_digest(body, "sha1").digest()
        if is_executable(info.st_mode):
            digest += EXEC_MARKER
    with _digests_lock:
        if len(_digests) >= _DIGEST_CACHE_MAX:
            _digests.clear()
        _digests[key] = digest
    return digest


def open_no_follow(root: Path, rel: str) -> io.BufferedReader:
    """Open a regular file below a folder for reading, without following a link at any part.

    Each folder on the way is opened from the one above it, so a listed
    folder that became a link since the listing cannot lead out of ``root``.

    Args:
        root: The folder; a link at its last part is refused.
        rel: The file's ``/``-joined path below ``root``.

    Returns:
        The open file; the caller closes it.

    Raises:
        OSError: When a part cannot be opened or is a link, or the file is
            not a regular file.
    """
    no_follow = getattr(os, "O_NOFOLLOW", 0)
    folder_flags = os.O_RDONLY | getattr(os, "O_DIRECTORY", 0) | no_follow
    parts = rel.split("/")
    folder = os.open(root, folder_flags)
    try:
        for part in parts[:-1]:
            below = os.open(part, folder_flags, dir_fd=folder)
            os.close(folder)
            folder = below
        # Without O_NONBLOCK, a pipe swapped in since the listing blocks the open until a writer comes.
        handle = os.open(parts[-1], os.O_RDONLY | no_follow | getattr(os, "O_NONBLOCK", 0), dir_fd=folder)
    finally:
        os.close(folder)
    try:
        if not stat.S_ISREG(os.fstat(handle).st_mode):
            raise OSError(errno.EINVAL, "Not a regular file", str(root / rel))
        return os.fdopen(handle, "rb")
    except BaseException:
        os.close(handle)
        raise


def _with_gitignore(folder: Path, rel: str, rules: list[tuple[str, list[_Rule]]]) -> list[tuple[str, list[_Rule]]]:
    """Add a folder's own ``.gitignore`` rules to the rules of the folders above it.

    Args:
        folder: The folder on disk.
        rel: Its ``/``-joined path below the root; empty for the root.
        rules: The rules of every folder above, highest first.

    Returns:
        ``rules``, with this folder's rules last when it has a readable ``.gitignore``.
    """
    try:
        # Opened without a check first, so a pipe or link swapped in cannot block or redirect the read.
        with open_no_follow(folder, GITIGNORE) as ignore_file:
            text = ignore_file.read().decode("utf-8", errors="replace")
    except OSError:
        return rules
    return [*rules, (rel, parse_gitignore(text))]


def _name_kept(name: str) -> bool:
    """Check the name rules of the listing, which need no look at the disk.

    Args:
        name: One path part.

    Returns:
        False for a ``.env*`` name, an operating system file or ``.git``.
    """
    # A worktree's or submodule's ``.git`` is a file, and it stays out too.
    return not (is_env_name(name) or is_os_name(name) or name == ".git")


def _kind_kept(path: Path, rel: str, is_dir: bool, is_file: bool, rules: list[tuple[str, list[_Rule]]]) -> bool:
    """Check the listing's rules for a child that is not a link and has a kept name.

    Args:
        path: The child on disk.
        rel: Its ``/``-joined path below the root.
        is_dir: Whether it is a folder, without following a link.
        is_file: Whether it is a regular file, without following a link.
        rules: The ``.gitignore`` rules of every folder above it, highest first.

    Returns:
        False for a socket, pipe or device, a skipped or virtual environment folder, or an ignored path.
    """
    if not (is_dir or is_file):
        return False
    if is_dir and (path.name in SKIPPED_DIRS or os.path.lexists(path / VENV_MARKER)):
        return False
    return not _ignored(rel, is_dir, rules)


def is_listed_file(root: str | os.PathLike[str], rel: str) -> bool:
    """Check that :func:`folder_entries` with no limit lists one file, without a walk of the whole folder.

    Each folder on the way gets the same checks as in the walk, and its
    ``.gitignore`` applies to the parts below it. Only the folders on the
    path are listed.

    Args:
        root: The folder.
        rel: The file's ``/``-joined path below ``root``.

    Returns:
        True when the full listing holds ``rel`` as a file.
    """
    folder = Path(root)
    if folder.is_symlink() or not folder.is_dir():
        return False
    parts = rel.split("/")
    if any(part in ("", ".", "..") for part in parts):
        return False
    rules: list[tuple[str, list[_Rule]]] = []
    current = ""
    for index, name in enumerate(parts):
        rules = _with_gitignore(folder, current, rules)
        # An exact name from the folder's own listing: a disk that ignores case would let ``IGNORED.TXT`` slip past a
        # case-sensitive rule, and the walk lists no child of a folder it cannot list.
        try:
            with os.scandir(folder) as found:
                if name not in {entry.name for entry in found}:
                    return False
        except OSError:
            return False
        child = folder / name
        child_rel = f"{current}/{name}" if current else name
        try:
            mode = child.lstat().st_mode
        except OSError:
            return False
        if stat.S_ISLNK(mode) or not _name_kept(name):
            return False
        is_dir, is_file = stat.S_ISDIR(mode), stat.S_ISREG(mode)
        if not _kind_kept(child, child_rel, is_dir, is_file, rules):
            return False
        if index == len(parts) - 1:
            return is_file
        if not is_dir:
            return False
        folder, current = child, child_rel
    return False


def _visit(
    folder: Path, rel: str, rules: list[tuple[str, list[_Rule]]], entries: list[dict[str, Any]], limit: int | None
) -> bool:
    """Add a folder's kept children to ``entries``, then walk into the kept folders.

    Args:
        folder: The folder on disk.
        rel: Its ``/``-joined path below the root; empty for the root.
        rules: The ``.gitignore`` rules of every folder above, highest first,
            each with the folder's path below the root.
        entries: The list to fill.
        limit: The most entries to list, or ``None``.

    Returns:
        False once ``limit`` is reached.
    """
    rules = _with_gitignore(folder, rel, rules)
    try:
        with os.scandir(folder) as found:
            children = sorted(found, key=lambda child: child.name)
    except OSError:
        return True
    for child in children:
        if child.is_symlink() or not _name_kept(child.name):
            continue
        child_rel = f"{rel}/{child.name}" if rel else child.name
        try:
            is_dir = child.is_dir(follow_symlinks=False)
            is_file = not is_dir and child.is_file(follow_symlinks=False)
        except OSError:
            continue
        if not _kind_kept(Path(child.path), child_rel, is_dir, is_file, rules):
            continue
        if limit is not None and len(entries) >= limit:
            return False
        if is_dir:
            entries.append({"path": child_rel, "type": "dir"})
            if not _visit(Path(child.path), child_rel, rules, entries, limit):
                return False
        else:
            try:
                size = child.stat(follow_symlinks=False).st_size
            except OSError:
                continue
            entries.append({"path": child_rel, "type": "file", "size": size})
    return True


def _ignored(rel: str, is_dir: bool, rules: list[tuple[str, list[_Rule]]]) -> bool:
    """Decide whether the ``.gitignore`` rules leave a path out.

    Args:
        rel: The path below the root.
        is_dir: Whether it is a folder.
        rules: The rules of every folder above, highest first.

    Returns:
        True when the last matching rule of the deepest file that matches ignores it.
    """
    for base, level in reversed(rules):
        sub = rel[len(base) + 1 :] if base else rel
        for rule in reversed(level):
            if rule.dir_only and not is_dir:
                continue
            if rule.regex.fullmatch(sub):
                return not rule.negated
    return False


def parse_gitignore(text: str) -> list[_Rule]:
    """Compile the lines of one ``.gitignore`` file.

    Args:
        text: The file's text.

    Returns:
        The rules, in file order. A line git cannot use, such as ``[z-a]``,
        gives no rule, as git skips it.
    """
    rules: list[_Rule] = []
    for raw in text.splitlines():
        line = raw.rstrip("\r")
        # Git drops trailing spaces unless a backslash escapes them.
        while line.endswith(" ") and not line.endswith("\\ "):
            line = line[:-1]
        if not line or line.startswith("#"):
            continue
        negated = line.startswith("!")
        if negated:
            line = line[1:]
        dir_only = line.endswith("/")
        line = line.rstrip("/")
        if not line:
            continue
        # Git anchors a pattern to its file's folder when a slash sits at its start or middle.
        anchored = "/" in line
        try:
            regex = re.compile(_translate(line.lstrip("/"), anchored), re.DOTALL)
        except re.error:
            continue
        rules.append(_Rule(regex, negated, dir_only))
    return rules


def _translate(pattern: str, anchored: bool) -> str:
    """Turn a ``.gitignore`` pattern into a regular expression for a path below its folder.

    Args:
        pattern: The pattern without its ``!``, leading slash or trailing slash.
        anchored: Whether it matches only from its file's folder.

    Returns:
        The expression, for :func:`re.fullmatch`.
    """
    parts = pattern.split("/")
    out = ""
    for index, part in enumerate(parts):
        last = index == len(parts) - 1
        if part == "**":
            out += ".+" if last else "(?:.+/)?"
            continue
        out += _glob(part)
        if not last:
            out += "/"
    return out if anchored else f"(?:.+/)?{out}"


def _glob(part: str) -> str:
    """Turn one path part of a ``.gitignore`` pattern into a regular expression.

    Args:
        part: The part, with no slash.

    Returns:
        The expression; ``*`` and ``?`` never match a slash.
    """
    out = ""
    index = 0
    while index < len(part):
        char = part[index]
        if char == "\\" and index + 1 < len(part):
            out += re.escape(part[index + 1])
            index += 2
            continue
        if char == "*":
            out += "[^/]*"
        elif char == "?":
            out += "[^/]"
        elif char == "[" and (end := part.find("]", index + 2)) != -1:
            body = part[index + 1 : end]
            negated = body[:1] in ("!", "^")
            body = body[1:] if negated else body
            out += "[" + ("^" if negated else "") + body.replace("\\", "\\\\") + "]"
            index = end
        else:
            out += re.escape(char)
        index += 1
    return out


__all__ = [
    "EXEC_MARKER",
    "FOLDER_ENTRIES_MAX",
    "GITIGNORE",
    "OS_NAMES",
    "SKIPPED_DIRS",
    "UNREADABLE_DIGEST",
    "VENV_MARKER",
    "FolderTreeError",
    "file_digest",
    "folder_entries",
    "folder_source_id",
    "is_env_name",
    "is_executable",
    "is_os_name",
    "open_no_follow",
    "parse_gitignore",
    "source_id",
]
