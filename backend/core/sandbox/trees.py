"""Provider-neutral parts of the tree-id sandbox layer (SKY-718).

A step runs in a box that a :class:`BoxSource` gives. The box gets its
workspace as a tree id from the :class:`TreeStore`, and gives back a new
tree id. The helpers here turn a tree into a tar archive and an archive into
a tree. The typed errors name the step that failed, so the caller knows which
step to retry or stop.
"""

from __future__ import annotations

import gzip
import io
import tarfile
import zlib
from collections.abc import Sequence
from typing import Protocol, TypeVar

from core.storage.tree_store import TreeEntry, TreeStore

from .grants import Grant

BoxT = TypeVar("BoxT")

_DIR_MODE = 0o755
_FILE_MODE = 0o644
_EXEC_MODE = 0o755
# A box's archive is untrusted input, so these limits keep one box from
# filling the worker's memory with a large file or a gzip bomb.
MAX_ARCHIVE_BYTES = 256 * 1024 * 1024
MAX_TREE_BYTES = 1024 * 1024 * 1024
MAX_TREE_MEMBERS = 100_000
MAX_NAME_BYTES = 4096
# Storing a tree recurses once per folder level, so a deep path must stop as a bad archive, not a RecursionError.
MAX_TREE_DEPTH = 256
# tarfile reads a long-name or pax header in one read, so a cap on each read
# stops a huge header before it is in memory. File data is read in smaller chunks.
_MAX_READ_BYTES = 4 * 1024 * 1024
_CHUNK_BYTES = 1024 * 1024
_GZIP_MAGIC = b"\x1f\x8b"


class BoxSource(Protocol[BoxT]):
    """Give a box to one agent step and take it back after the step."""

    def acquire(self, agent_id: str, grants: Sequence[Grant]) -> BoxT:
        """Return a box whose network policy is the grant list.

        Args:
            agent_id: The agent that runs the step.
            grants: The grants of the step; empty means no network.

        Returns:
            The open box.
        """
        ...

    def release(self, box: BoxT, healthy: bool) -> None:
        """Take a box back after a step.

        Args:
            box: A box that :meth:`acquire` gave.
            healthy: False when the box failed, so no later step may use it.
        """
        ...


class TreeSandboxError(RuntimeError):
    """Signal a step that did not finish in its box."""

    def __init__(self, step_id: str, message: str) -> None:
        """Keep the id of the step that failed.

        Args:
            step_id: The step id, ``run_id:agent_id:seq``.
            message: What went wrong, without box output.
        """
        super().__init__(f"Step {step_id}: {message}")
        self.step_id = step_id


class FenceLostError(TreeSandboxError):
    """Signal a step whose run generation is no longer current."""


class GrantDeniedError(TreeSandboxError):
    """Signal a grant list that does not resolve to a network policy."""


class BoxFailureError(TreeSandboxError):
    """Signal a box that failed or died; the caller can retry the step on a new box."""


class MetricFailedError(TreeSandboxError):
    """Signal a metric command that ran but gave no score."""


class TreeRejectedError(TreeSandboxError):
    """Signal a tree that the layer refuses; a retry gives the same tree, so do not retry."""


class BadArchiveError(ValueError):
    """Signal an archive that is not a valid tree or is over a size limit."""


class _CountingReader:
    """Read an archive stream and stop at the unpacked size and single-read limits."""

    def __init__(self, raw: io.BufferedIOBase | gzip.GzipFile) -> None:
        """Wrap the stream that gives the unpacked tar bytes.

        Args:
            raw: A seekable stream of the tar bytes.
        """
        self._raw = raw

    def read(self, size: int = -1) -> bytes:
        """Return up to ``size`` bytes of the tar stream.

        Args:
            size: The number of bytes; a negative value is refused.

        Returns:
            The bytes read.

        Raises:
            BadArchiveError: When one read or the total is over its limit.
        """
        if size < 0 or size > _MAX_READ_BYTES:
            raise BadArchiveError("The archive has a header larger than the read limit.")
        data = self._raw.read(size)
        self._check()
        return data

    def seek(self, offset: int, whence: int = io.SEEK_SET) -> int:
        """Move in the tar stream.

        Args:
            offset: The new position, relative to ``whence``.
            whence: The ``io`` seek origin.

        Returns:
            The new position.

        Raises:
            BadArchiveError: When the target is past the size limit.
        """
        # A forward seek on a gzip stream unpacks every byte up to the target, so a
        # member of an unknown type with a huge size is refused before the seek.
        if whence in (io.SEEK_SET, io.SEEK_CUR):
            self._check(offset if whence == io.SEEK_SET else self._raw.tell() + offset)
        position = self._raw.seek(offset, whence)
        self._check(position)
        return position

    def tell(self) -> int:
        """Return the position in the tar stream."""
        return self._raw.tell()

    def _check(self, position: int | None = None) -> None:
        """Refuse a stream position past the size limit.

        Args:
            position: The position to check; None checks the current one.

        Raises:
            BadArchiveError: When the position is past the limit.
        """
        # Each member gets 1 KiB for its header and block padding. A long-name or pax header
        # can be larger, so long names count against the size limit.
        if (self._raw.tell() if position is None else position) > MAX_TREE_BYTES + MAX_TREE_MEMBERS * 1024:
            raise BadArchiveError(f"The archive unpacks to more than {MAX_TREE_BYTES} bytes.")


def tree_to_tar(store: TreeStore, run_id: str, tree_id: str) -> bytes:
    """Return a gzip tar archive of a stored tree.

    Args:
        store: The tree store that holds the tree.
        run_id: The run that reads the tree.
        tree_id: The tree's object id.

    Returns:
        The archive bytes; the tree's entries are at its top level.

    Raises:
        LookupError: When the run cannot see the tree or one of its objects.
    """
    buffer = io.BytesIO()
    with tarfile.open(fileobj=buffer, mode="w:gz") as archive:
        _add_tree(store, run_id, tree_id, "", archive)
    return buffer.getvalue()


def _add_tree(store: TreeStore, run_id: str, tree_id: str, prefix: str, archive: tarfile.TarFile) -> None:
    """Add one tree and its subtrees to an archive.

    Args:
        store: The tree store that holds the tree.
        run_id: The run that reads the tree.
        tree_id: The tree's object id.
        prefix: The tree's path in the archive, empty or ending in ``/``.
        archive: The archive to add to.

    Raises:
        LookupError: When the run cannot see the tree or one of its objects.
    """
    entries = store.get_tree(run_id, tree_id)
    if entries is None:
        raise LookupError(f"Run {run_id} cannot see tree {tree_id}.")
    for entry in entries:
        info = tarfile.TarInfo(prefix + entry.name)
        if entry.mode == "tree":
            info.type, info.mode = tarfile.DIRTYPE, _DIR_MODE
            archive.addfile(info)
            _add_tree(store, run_id, entry.object_id, f"{info.name}/", archive)
            continue
        data = store.get_blob(run_id, entry.object_id)
        if data is None:
            raise LookupError(f"Run {run_id} cannot see blob {entry.object_id}.")
        if entry.mode == "symlink":
            info.type, info.linkname = tarfile.SYMTYPE, data.decode("utf-8")
            archive.addfile(info)
        else:
            info.size, info.mode = len(data), _EXEC_MODE if entry.mode == "exec" else _FILE_MODE
            archive.addfile(info, io.BytesIO(data))


def tar_to_tree(store: TreeStore, run_id: str, data: bytes) -> str:
    """Store the files of a tar archive as a tree and return its id.

    Args:
        store: The tree store to write to.
        run_id: The run that owns the new objects.
        data: A tar archive, plain or gzip.

    Returns:
        The tree id of the archive's top level.

    Raises:
        BadArchiveError: When the archive is over a size limit, does not
            read, a member path leaves the top level or is too deep, a name
            is both a file and a folder, a name is too long or not UTF-8, or
            a hard link names no earlier file.
    """
    if len(data) > MAX_ARCHIVE_BYTES:
        raise BadArchiveError(f"The archive is larger than {MAX_ARCHIVE_BYTES} bytes.")
    raw = gzip.GzipFile(fileobj=io.BytesIO(data)) if data[:2] == _GZIP_MAGIC else io.BytesIO(data)
    root: dict[str, object] = {}
    members = total = 0
    try:
        with tarfile.open(fileobj=_CountingReader(raw), mode="r:") as archive:  # type: ignore[call-overload]
            for member in archive:
                members += 1
                if members > MAX_TREE_MEMBERS:
                    raise BadArchiveError(f"The archive has more than {MAX_TREE_MEMBERS} members.")
                if _byte_length(member.name) > MAX_NAME_BYTES or _byte_length(member.linkname) > MAX_NAME_BYTES:
                    raise BadArchiveError(f"An archive member name is longer than {MAX_NAME_BYTES} bytes.")
                parts = _parts(member.name)
                if not parts:
                    continue
                if len(parts) > MAX_TREE_DEPTH:
                    raise BadArchiveError(f"An archive member path is deeper than {MAX_TREE_DEPTH} levels.")
                folder = root
                for part in parts[:-1]:
                    folder = folder.setdefault(part, {})  # type: ignore[assignment]
                    if not isinstance(folder, dict):
                        raise BadArchiveError(f"Archive member {member.name!r} is under a file.")
                name = parts[-1]
                if member.isdir():
                    if not isinstance(folder.setdefault(name, {}), dict):
                        raise BadArchiveError(f"Archive member {member.name!r} is a file and a folder.")
                    continue
                if not (member.issym() or member.islnk() or member.isreg()):
                    # The tree format has no device, pipe or socket entry, so a box's
                    # special files do not survive a step.
                    continue
                if isinstance(folder.get(name), dict):
                    raise BadArchiveError(f"Archive member {member.name!r} is a folder and a file.")
                if member.issym():
                    folder[name] = ("symlink", store.put_blob(run_id, member.linkname.encode("utf-8")))
                elif member.islnk():
                    # The target's stored entry, so a missing target is a bad archive, not a KeyError.
                    folder[name] = _stored_file(root, member)
                else:
                    # The header size is what the read returns, so the check comes before the read.
                    total += member.size
                    if total > MAX_TREE_BYTES:
                        raise BadArchiveError(f"The archive unpacks to more than {MAX_TREE_BYTES} bytes.")
                    mode = "exec" if member.mode & 0o111 else "file"
                    folder[name] = (mode, store.put_blob(run_id, _read_member(archive, member)))
        return _put_folder(store, run_id, root)
    except BadArchiveError:
        raise
    except (tarfile.TarError, EOFError, OSError, zlib.error) as error:
        raise BadArchiveError(f"The archive does not read ({type(error).__name__}).") from error
    except ValueError as error:
        # tarfile keeps a name that is not UTF-8 as surrogates, which the tree store cannot encode.
        raise BadArchiveError(f"The archive does not store ({type(error).__name__}).") from error


def _byte_length(name: str) -> int:
    """Return the length of a member name in bytes, as the archive holds it.

    Args:
        name: The member name or link target.

    Returns:
        The number of bytes.
    """
    # tarfile keeps a non-UTF-8 byte as a surrogate, which surrogateescape turns back into one byte.
    return len(name.encode("utf-8", "surrogateescape"))


def _read_member(archive: tarfile.TarFile, member: tarfile.TarInfo) -> bytearray:
    """Return a regular member's content, read in chunks under the read limit.

    Args:
        archive: The open archive.
        member: A regular-file member.

    Returns:
        The member's bytes, in a bytearray.

    Raises:
        BadArchiveError: When the member ends before its header size.
    """
    handle = archive.extractfile(member)
    if handle is None:
        return bytearray()
    # The read holds one buffer of the member size, not a list of chunks plus a joined copy.
    data = bytearray(member.size)
    with memoryview(data) as view:
        filled = 0
        while filled < member.size:
            count = handle.readinto(view[filled : filled + _CHUNK_BYTES])
            if not count:
                raise BadArchiveError(f"Archive member {member.name!r} is shorter than its header says.")
            filled += count
    # A bytearray, not bytes: bytes(data) would add one more full copy.
    return data


def _parts(name: str) -> list[str]:
    """Split an archive path into its names; a leading ``/`` is dropped.

    Args:
        name: The member path.

    Returns:
        The names, without empty and ``.`` parts.

    Raises:
        BadArchiveError: When the path holds ``..``.
    """
    parts = [part for part in name.split("/") if part not in {"", "."}]
    if ".." in parts:
        raise BadArchiveError(f"Archive member {name!r} leaves the tree.")
    return parts


def _stored_file(root: dict[str, object], member: tarfile.TarInfo) -> tuple[str, str]:
    """Return the stored entry that a hard link names.

    Args:
        root: The folders stored so far.
        member: The hard-link member.

    Returns:
        The target's ``(mode, blob id)`` pair.

    Raises:
        BadArchiveError: When no earlier file has the target path.
    """
    value: object = root
    for part in _parts(member.linkname):
        value = value.get(part) if isinstance(value, dict) else None
    if not isinstance(value, tuple) or value[0] == "symlink":
        raise BadArchiveError(f"Hard link {member.name!r} names no earlier file.")
    return value


def _put_folder(store: TreeStore, run_id: str, folder: dict[str, object]) -> str:
    """Store one folder and its subfolders as trees.

    Args:
        store: The tree store to write to.
        run_id: The run that owns the new objects.
        folder: Maps each name to a subfolder dict or a ``(mode, blob id)`` pair.

    Returns:
        The folder's tree id.
    """
    entries = []
    for name, value in folder.items():
        if isinstance(value, dict):
            entries.append(TreeEntry(name, "tree", _put_folder(store, run_id, value)))
        else:
            mode, object_id = value  # type: ignore[misc]
            entries.append(TreeEntry(name, mode, object_id))
    return store.put_tree(run_id, entries)
