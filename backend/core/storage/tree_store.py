"""Content-addressed workspace trees on top of the step log (SKY-710).

Tree format, version 1 (``workspaces.storage_version`` 1). There are two
object types. A blob is ``b"blob\\0"`` followed by the file bytes. A tree is
``b"tree\\0"`` followed by the canonical JSON list of its entries, each
``[name, mode, object_id]``, sorted by name. The object id is the SHA-256 hex
digest of the whole stored object, so the same content always gets the same
id.

Objects travel through the :class:`StepLogStore` buffer, in the same packs
and flushes as the steps, and each one counts toward its account's stored
bytes. Nothing deletes them yet; the tree retention issue (SKY-733) adds
deletion, also on account deletion. A read names its run: before the
flush only that run sees the object, after it every run of the same account.
"""

from __future__ import annotations

import hashlib
import json
from collections.abc import Iterable
from dataclasses import dataclass

from .step_log_store import OBJECT_ID, ObjectTooLargeError, StepLogStore

TREE_MODES = frozenset({"file", "exec", "symlink", "tree"})
_BLOB_HEADER = b"blob\0"
_TREE_HEADER = b"tree\0"


@dataclass(frozen=True)
class TreeEntry:
    """One name in a tree: a file, an executable, a symlink or a subtree."""

    name: str
    mode: str
    object_id: str


def object_id_of(stored: bytes) -> str:
    """Return the content id of a stored object.

    Args:
        stored: The object bytes, header included.

    Returns:
        The SHA-256 hex digest.
    """
    return hashlib.sha256(stored).hexdigest()


def encode_blob(data: bytes) -> bytes:
    """Return the stored form of a file's bytes.

    Args:
        data: The file bytes.

    Returns:
        The blob header and the bytes.
    """
    return _BLOB_HEADER + data


def encode_tree(entries: Iterable[TreeEntry]) -> bytes:
    """Return the canonical stored form of a tree.

    Args:
        entries: The tree's entries, in any order.

    Returns:
        The tree header and the sorted JSON entry list.

    Raises:
        ValueError: When a name is empty, repeated, ``.``, ``..`` or holds
            ``/`` or a NUL byte, or a mode or object id is not valid.
    """
    rows = sorted((entry.name, entry.mode, entry.object_id) for entry in entries)
    names = [name for name, _, _ in rows]
    if len(set(names)) != len(names):
        raise ValueError("a tree holds the same name twice")
    for name, mode, object_id in rows:
        if name in {"", ".", ".."} or "/" in name or "\0" in name:
            raise ValueError(f"bad tree entry name: {name!r}")
        if mode not in TREE_MODES:
            raise ValueError(f"bad tree entry mode: {mode!r}")
        if not OBJECT_ID.fullmatch(object_id):
            raise ValueError(f"bad tree entry object id: {object_id!r}")
    body = json.dumps([list(row) for row in rows], separators=(",", ":"), ensure_ascii=False)
    return _TREE_HEADER + body.encode("utf-8")


def decode_tree(stored: bytes) -> list[TreeEntry]:
    """Return the entries of a stored tree.

    Args:
        stored: The tree object bytes.

    Returns:
        The entries, sorted by name.

    Raises:
        ValueError: When the bytes are not a tree object.
    """
    if not stored.startswith(_TREE_HEADER):
        raise ValueError("not a tree object")
    return [TreeEntry(name, mode, object_id) for name, mode, object_id in json.loads(stored[len(_TREE_HEADER) :])]


class TreeStore:
    """Write and read content-addressed blobs and trees through one worker's step log."""

    def __init__(self, step_log: StepLogStore) -> None:
        """Bind the tree store to the worker's step log.

        Args:
            step_log: The step log whose buffer and flushes carry the objects.
        """
        self._step_log = step_log

    def put_blob(self, run_id: str, data: bytes) -> str:
        """Buffer one file's bytes and return its content id.

        Args:
            run_id: The job whose owner owns the blob.
            data: The file bytes.

        Returns:
            The blob's object id.

        Raises:
            ValueError: When ``run_id`` is not a valid run id.
        """
        stored = encode_blob(data)
        object_id = object_id_of(stored)
        self._step_log.put_object(run_id, object_id, stored)
        return object_id

    def put_tree(self, run_id: str, entries: Iterable[TreeEntry]) -> str:
        """Buffer one tree and return its content id, the tree id a step names.

        Args:
            run_id: The job whose owner owns the tree.
            entries: The tree's entries.

        Returns:
            The tree's object id.

        Raises:
            ValueError: When an entry is not valid (see :func:`encode_tree`)
                or ``run_id`` is not a valid run id.
        """
        stored = encode_tree(entries)
        object_id = object_id_of(stored)
        self._step_log.put_object(run_id, object_id, stored)
        return object_id

    def get_blob(self, run_id: str, object_id: str, max_bytes: int | None = None) -> bytes | None:
        """Return a blob's file bytes for a run, from the buffer before its flush.

        Args:
            run_id: The job that reads the blob.
            object_id: The blob's object id.
            max_bytes: The most file bytes to load; ``None`` loads the blob at any size. The step log compares
                the stored length to the cap plus the blob header before it loads, so a larger blob never loads.

        Returns:
            The file bytes, or ``None`` when the run cannot see such an object.

        Raises:
            ObjectTooLargeError: When the blob has more than ``max_bytes`` file bytes; its ``size`` is the file
                length.
            ValueError: When the stored bytes do not match the id or are not a blob.
        """
        try:
            stored = self._get(run_id, object_id, None if max_bytes is None else max_bytes + len(_BLOB_HEADER))
        except ObjectTooLargeError as error:
            raise ObjectTooLargeError(object_id, error.size - len(_BLOB_HEADER)) from None
        if stored is None:
            return None
        if not stored.startswith(_BLOB_HEADER):
            raise ValueError(f"object {object_id} is not a blob")
        return stored[len(_BLOB_HEADER) :]

    def get_tree(self, run_id: str, object_id: str) -> list[TreeEntry] | None:
        """Return a tree's entries for a run, from the buffer before its flush.

        Args:
            run_id: The job that reads the tree.
            object_id: The tree's object id.

        Returns:
            The entries, or ``None`` when the run cannot see such an object.

        Raises:
            ValueError: When the stored bytes do not match the id or are not a tree.
        """
        stored = self._get(run_id, object_id)
        return None if stored is None else decode_tree(stored)

    def _get(self, run_id: str, object_id: str, max_bytes: int | None = None) -> bytes | None:
        """Return an object's stored bytes for a run after a check that they hash to the id.

        Args:
            run_id: The job that reads the object.
            object_id: The object id.
            max_bytes: The most stored bytes to load; ``None`` loads the object at any size.

        Returns:
            The stored bytes, or ``None`` when the run cannot see such an object.

        Raises:
            ObjectTooLargeError: When the stored bytes are longer than ``max_bytes``.
            ValueError: When the bytes do not hash to the id.
        """
        stored = self._step_log.get_object(run_id, object_id, max_bytes)
        if stored is not None and object_id_of(stored) != object_id:
            raise ValueError(f"object {object_id} does not match its content")
        return stored
