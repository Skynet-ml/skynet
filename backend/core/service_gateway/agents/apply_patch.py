"""Apply a Codex ``apply_patch`` patch to the setup agent's copy of a local folder (SKY-997).

The patch format is the one Codex CLI's ``apply_patch`` tool takes::

    *** Begin Patch
    *** Add File: docs/new.md
    +text of the new file
    *** Update File: src/app.py
    *** Move to: src/main.py
    @@ def main():
     context line
    -removed line
    +added line
    *** End of File
    *** Delete File: old.txt
    *** End Patch

The patch applies in two steps. The first step reads each file and computes
every new text, so a bad path or a hunk that does not match changes nothing
and returns the reason. The second step writes the files.

This module runs outside the command sandbox, and a command can put a link
in the copy. So every read and write walks the path one folder at a time from
the copy's root with ``O_NOFOLLOW``, and a link at any part is refused. A
write goes to a new file that then replaces the name, so a hard link at the
name is unlinked, never written through.
"""

from __future__ import annotations

import contextlib
import errno
import os
import stat
from collections.abc import Iterator
from dataclasses import dataclass, field
from pathlib import Path
from uuid import uuid4

from ...local.folder_tree import is_env_name, open_no_follow
from ...models.blackbox import clean_draft_path

APPLY_PATCH_TOOL = "apply_patch"
BEGIN = "*** Begin Patch"
END = "*** End Patch"
ADD = "*** Add File: "
UPDATE = "*** Update File: "
DELETE = "*** Delete File: "
MOVE = "*** Move to: "
END_OF_FILE = "*** End of File"
HUNK = "@@"
NEW_FILE_MODE = 0o644
_FOLDER_FLAGS = os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW


class PatchError(ValueError):
    """A patch that cannot apply; the message says why, for the model."""


@dataclass
class _Chunk:
    """One ``@@`` part of an update: the lines it finds and the lines that replace them."""

    context: str | None
    old: list[str] = field(default_factory=list)
    new: list[str] = field(default_factory=list)
    end_of_file: bool = False


@dataclass
class _Op:
    """One file operation of a patch."""

    kind: str
    path: str
    lines: list[str] = field(default_factory=list)
    move_to: str | None = None
    chunks: list[_Chunk] = field(default_factory=list)


def check_path(raw: str) -> str:
    """Check one path a patch names, relative to the copy's root.

    Args:
        raw: The path as the patch gives it.

    Returns:
        The clean ``/``-joined path.

    Raises:
        PatchError: For an absolute path, a path that leaves the copy, a
            ``.git`` part, or a ``.env*`` name.
    """
    text = raw.strip()
    if text.startswith(("/", "~")):
        raise PatchError(f"{text}: use a path relative to the folder copy; a path outside the copy is refused.")
    try:
        clean = clean_draft_path(text)
    except ValueError as error:
        raise PatchError(f"{text}: {error}") from error
    if any(is_env_name(part) for part in clean.split("/")):
        raise PatchError(f"{clean}: .env files stay out of the copy. Ask the user to add the value as a saved secret.")
    return clean


def parse_patch(text: str) -> list[_Op]:
    """Parse a Codex patch into file operations.

    Args:
        text: The whole patch, from ``*** Begin Patch`` to ``*** End Patch``.

    Returns:
        The operations, in patch order.

    Raises:
        PatchError: When the patch does not follow the format.
    """
    lines = [line.removesuffix("\r") for line in text.strip().split("\n")]
    if not lines or lines[0].strip() != BEGIN:
        raise PatchError(f"The first line of the patch must be '{BEGIN}'.")
    if lines[-1].strip() != END:
        raise PatchError(f"The last line of the patch must be '{END}'.")
    body = lines[1:-1]
    ops: list[_Op] = []
    index = 0
    while index < len(body):
        line = body[index]
        index += 1
        if line.startswith(ADD):
            op = _Op("add", check_path(line[len(ADD) :]))
            while index < len(body) and body[index].startswith("+"):
                op.lines.append(body[index][1:])
                index += 1
            ops.append(op)
        elif line.startswith(DELETE):
            ops.append(_Op("delete", check_path(line[len(DELETE) :])))
        elif line.startswith(UPDATE):
            op = _Op("update", check_path(line[len(UPDATE) :]))
            if index < len(body) and body[index].startswith(MOVE):
                op.move_to = check_path(body[index][len(MOVE) :])
                index += 1
            index = _parse_chunks(op, body, index)
            ops.append(op)
        elif line.strip():
            raise PatchError(f"Line {index + 1}: expected '*** Add File:', '*** Update File:' or '*** Delete File:'.")
    if not ops:
        raise PatchError("The patch changes no file.")
    return ops


def _parse_chunks(op: _Op, body: list[str], index: int) -> int:
    """Read an update's ``@@`` chunks until the next file header or the end.

    Args:
        op: The update operation to fill.
        body: The patch lines between its first and last line.
        index: The index of the update's first chunk line in ``body``.

    Returns:
        The index of the line after the update.

    Raises:
        PatchError: For an update with no chunk, or a chunk line with no
            ``' '``, ``'-'`` or ``'+'`` prefix.
    """
    chunk: _Chunk | None = None
    while index < len(body):
        line = body[index]
        if line.startswith("*** ") and not line.startswith(END_OF_FILE):
            break
        index += 1
        if line.startswith(HUNK):
            chunk = _Chunk(context=line[len(HUNK) :].strip() or None)
            op.chunks.append(chunk)
        elif line.startswith(END_OF_FILE):
            if chunk is None:
                raise PatchError(f"Line {index + 1}: '{END_OF_FILE}' needs a chunk before it.")
            chunk.end_of_file = True
        else:
            if chunk is None:
                # Codex lets the first chunk of an update skip its ``@@`` line.
                chunk = _Chunk(context=None)
                op.chunks.append(chunk)
            prefix, rest = (line[:1], line[1:]) if line else (" ", "")
            if prefix == " ":
                chunk.old.append(rest)
                chunk.new.append(rest)
            elif prefix == "-":
                chunk.old.append(rest)
            elif prefix == "+":
                chunk.new.append(rest)
            else:
                raise PatchError(f"Line {index + 1}: a chunk line must start with ' ', '-' or '+'.")
    if not op.chunks and op.move_to is None:
        raise PatchError(f"{op.path}: the update has no '@@' chunk.")
    return index


def _seek(lines: list[str], pattern: list[str], start: int, end_of_file: bool) -> int:
    """Find where ``pattern`` starts in ``lines``, at or after ``start``.

    As Codex does, an exact match wins, then a match that ignores trailing
    spaces, then one that ignores spaces at both ends.

    Args:
        lines: The file's lines.
        pattern: The lines to find.
        start: The first index to try.
        end_of_file: Try the match at the end of the file first.

    Returns:
        The index, or -1 when the lines are not there.
    """
    if not pattern:
        return start
    last = len(lines) - len(pattern)
    if last < start:
        return -1
    for norm in (lambda s: s, str.rstrip, str.strip):
        wanted = [norm(p) for p in pattern]
        if end_of_file and [norm(x) for x in lines[last:]] == wanted:
            return last
        for index in range(start, last + 1):
            if [norm(x) for x in lines[index : index + len(pattern)]] == wanted:
                return index
    return -1


def updated_text(path: str, text: str, chunks: list[_Chunk]) -> str:
    """Apply an update's chunks to one file's text.

    Args:
        path: The file path, for the error text.
        text: The file's current text.
        chunks: The update's chunks, in file order.

    Returns:
        The new text; it ends with a newline.

    Raises:
        PatchError: When a chunk's context or lines are not in the file.
    """
    lines = text.split("\n")
    if lines and lines[-1] == "":
        lines.pop()
    replacements: list[tuple[int, int, list[str]]] = []
    cursor = 0
    for chunk in chunks:
        if chunk.context is not None:
            found = _seek(lines, [chunk.context], cursor, False)
            if found < 0:
                raise PatchError(f"{path}: the context line '{chunk.context}' is not in the file.")
            cursor = found + 1
        if not chunk.old:
            # A chunk that only adds lines adds them at the end, as in Codex.
            replacements.append((len(lines), 0, chunk.new))
            continue
        found = _seek(lines, chunk.old, cursor, chunk.end_of_file)
        old = chunk.old
        if found < 0 and old and old[-1] == "":
            # A chunk often ends with an empty context line that the file has as its final newline.
            old = old[:-1]
            found = _seek(lines, old, cursor, chunk.end_of_file)
        if found < 0:
            shown = "\n".join(chunk.old[:5])
            raise PatchError(f"{path}: these lines are not in the file (at or after line {cursor + 1}):\n{shown}")
        new = chunk.new[:-1] if old is not chunk.old and chunk.new and chunk.new[-1] == "" else chunk.new
        replacements.append((found, len(old), new))
        cursor = found + len(old)
    for start, length, new in sorted(replacements, key=lambda item: item[0], reverse=True):
        lines[start : start + length] = new
    return "\n".join(lines) + "\n" if lines else ""


@contextlib.contextmanager
def _folder_of(root: Path, rel: str, *, create: bool) -> Iterator[tuple[int, str]]:
    """Open the folder that holds a path, walking from ``root`` without following a link.

    Args:
        root: The copy's root.
        rel: The clean ``/``-joined path.
        create: Make missing folders on the way.

    Yields:
        The open folder's descriptor and the path's last part.

    Raises:
        OSError: When a part is a link or not a folder, or is missing and
            ``create`` is False.
    """
    *folders, name = rel.split("/")
    fd = os.open(root, _FOLDER_FLAGS)
    try:
        for folder in folders:
            if create:
                with contextlib.suppress(FileExistsError):
                    os.mkdir(folder, dir_fd=fd)
            child = os.open(folder, _FOLDER_FLAGS, dir_fd=fd)
            os.close(fd)
            fd = child
        yield fd, name
    finally:
        os.close(fd)


def write_inside(root: Path, rel: str, data: bytes, mode: int = NEW_FILE_MODE) -> None:
    """Write a file below ``root`` without following a link at any part.

    Args:
        root: The copy's root.
        rel: The clean ``/``-joined path.
        data: The file's bytes.
        mode: The file's permission bits.

    Raises:
        OSError: When a part of the path is a link or not a folder.
    """
    with _folder_of(root, rel, create=True) as (fd, name):
        with contextlib.suppress(FileNotFoundError):
            if stat.S_ISDIR(os.stat(name, dir_fd=fd, follow_symlinks=False).st_mode):
                raise IsADirectoryError(errno.EISDIR, "A folder has this name", rel)
        temp = f".{name}.{uuid4().hex}"
        out = os.open(temp, os.O_WRONLY | os.O_CREAT | os.O_EXCL | os.O_NOFOLLOW, 0o600, dir_fd=fd)
        try:
            with os.fdopen(out, "wb") as handle:
                handle.write(data)
                os.fchmod(handle.fileno(), mode & 0o777)
            os.replace(temp, name, src_dir_fd=fd, dst_dir_fd=fd)
        except OSError:
            with contextlib.suppress(OSError):
                os.unlink(temp, dir_fd=fd)
            raise


def remove_inside(root: Path, rel: str) -> None:
    """Delete one regular file below ``root`` without following a link at any part.

    Args:
        root: The copy's root.
        rel: The clean ``/``-joined path.

    Raises:
        OSError: When a part of the path is a link, or the file is missing or
            not a regular file.
    """
    with _folder_of(root, rel, create=False) as (fd, name):
        if not stat.S_ISREG(os.stat(name, dir_fd=fd, follow_symlinks=False).st_mode):
            raise OSError(errno.EINVAL, "Not a regular file", rel)
        os.unlink(name, dir_fd=fd)


def _check_parents(root: Path, rel: str) -> None:
    """Check that no existing folder on a path is a link, before the patch writes there.

    Args:
        root: The copy's root.
        rel: The clean ``/``-joined path.

    Raises:
        PatchError: When a part is a link or a file.
    """
    current = root
    for part in rel.split("/")[:-1]:
        current = current / part
        if not os.path.lexists(current):
            return
        if current.is_symlink() or not current.is_dir():
            raise PatchError(f"{rel}: {part} is a link or a file, so the patch cannot write there.")
    target = root / rel
    if target.is_symlink():
        raise PatchError(f"{rel}: the path is a link, and links are refused.")
    if target.is_dir():
        raise PatchError(f"{rel}: the path is a folder.")


def _read(root: Path, rel: str) -> tuple[str, int]:
    """Read one text file of the copy without following a link.

    Args:
        root: The copy's root.
        rel: The clean path.

    Returns:
        The text and the file's permission bits.

    Raises:
        PatchError: When the file is missing, a link, not a regular file or not UTF-8 text.
    """
    try:
        with open_no_follow(root, rel) as handle:
            mode = os.fstat(handle.fileno()).st_mode
            data = handle.read()
    except OSError as error:
        reason = "it is a link" if error.errno == errno.ELOOP else "it is missing or not a regular file"
        raise PatchError(f"{rel}: the file cannot be read: {reason}.") from error
    try:
        return data.decode("utf-8"), stat.S_IMODE(mode)
    except UnicodeDecodeError as error:
        raise PatchError(f"{rel}: the file is not UTF-8 text.") from error


def apply_patch(root: Path, patch: str) -> list[str]:
    """Apply a Codex patch to the copy.

    Every file is checked and planned before the first write, so a
    :class:`PatchError` changes nothing. The writes then run one file at a
    time, so an :class:`OSError` in a write leaves the earlier files changed.

    Args:
        root: The copy's root folder.
        patch: The patch text.

    Returns:
        One line for each changed file, such as ``"M src/app.py"``.

    Raises:
        PatchError: For a patch that does not parse, a refused path, a link,
            a missing file or a hunk that does not match; the copy is then
            unchanged.
        OSError: When a write fails part way; the files written before it stay changed.
    """
    ops = parse_patch(patch)
    # Path to its new bytes and mode, or None for a delete, so later ops see earlier ones.
    planned: dict[str, tuple[bytes, int] | None] = {}
    summary: list[str] = []

    def current(rel: str) -> tuple[str, int] | None:
        """Return a file's text and mode as the patch left it so far.

        Args:
            rel: The clean path.

        Returns:
            The text and mode, or None when the patch deleted the file.
        """
        if rel in planned:
            item = planned[rel]
            return None if item is None else (item[0].decode("utf-8"), item[1])
        return _read(root, rel)

    for op in ops:
        _check_parents(root, op.path)
        if op.kind == "add":
            planned[op.path] = (("\n".join(op.lines) + "\n").encode("utf-8"), NEW_FILE_MODE)
            summary.append(f"A {op.path}")
            continue
        found = current(op.path)
        if found is None:
            raise PatchError(f"{op.path}: the patch already deleted this file.")
        text, mode = found
        if op.kind == "delete":
            planned[op.path] = None
            summary.append(f"D {op.path}")
            continue
        new_text = updated_text(op.path, text, op.chunks) if op.chunks else text
        if op.move_to is not None and op.move_to != op.path:
            _check_parents(root, op.move_to)
            planned[op.path] = None
            planned[op.move_to] = (new_text.encode("utf-8"), mode)
            summary.append(f"R {op.path} -> {op.move_to}")
        else:
            planned[op.path] = (new_text.encode("utf-8"), mode)
            summary.append(f"M {op.path}")
    for rel, item in planned.items():
        if item is None:
            with contextlib.suppress(FileNotFoundError):
                remove_inside(root, rel)
        else:
            write_inside(root, rel, item[0], item[1])
    return summary


__all__ = [
    "APPLY_PATCH_TOOL",
    "PatchError",
    "apply_patch",
    "check_path",
    "parse_patch",
    "remove_inside",
    "updated_text",
    "write_inside",
]
