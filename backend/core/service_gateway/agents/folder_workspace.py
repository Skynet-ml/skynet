"""Give the Mac app's setup agent a private copy of the user's local folder (SKY-997).

On the Metric and Check screens of a local-folder run, the setup agent runs
shell commands and applies patches. They work on a copy of the folder, never
on the folder itself. Each setup chat gets one copy in the app's state folder,
at ``setup-workspaces/<chat id>/folder``. The copy holds the files of the
SKY-990 listing (:func:`~core.local.folder_tree.folder_entries`), so ``.env*``
files, links, git data, installed packages and ignored files never reach it.
A command gets the saved secrets that have hosts only through the SKY-996
egress proxy, as placeholders.

The commands run in :class:`~.local_command_runner.LocalCommandRunner` in
proxy mode. Its workspace is the chat's session folder, so ``HOME``, the
temporary folder and tool caches stay outside the copy; the commands start in
the copy. After each tool call :meth:`FolderWorkspace.changes` compares the
copy with the folder as it was copied, and the changed files become the
setup draft's files, and the deleted ones become its deleted files.

The chat's attached files go to ``attachments/<id>/<name>`` in the session
folder, next to the copy and never in it, so a command reads them at
``../attachments/<id>/<name>`` and they never become drafts.

The copy is deleted when the chat is deleted, when its run is submitted, and
when the app starts (:func:`sweep_stale_copies`).
"""

from __future__ import annotations

import contextlib
import hashlib
import logging
import os
import re
import shutil
import tempfile
import threading
from collections.abc import Callable, Iterable, Mapping
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

from ...config import settings
from ...connectors.saved_secrets import SavedSecretStore
from ...local.folder_tree import (
    EXEC_MARKER,
    FOLDER_ENTRIES_MAX,
    FolderTreeError,
    file_digest,
    folder_entries,
    folder_source_id,
    is_env_name,
    is_executable,
    open_no_follow,
)
from ...models.blackbox import DRAFT_FILE_BYTES, clean_draft_path
from .apply_patch import apply_patch, remove_inside, write_inside
from .local_command_runner import LocalCommandRunner
from .local_egress_proxy import LocalEgressProxy, SetupSecret

logger = logging.getLogger(__name__)

SESSIONS_DIR = "setup-workspaces"
EGRESS_DIR = "setup-egress"
COPY_DIR = "folder"
ATTACHMENTS_DIR = "attachments"
CLOSED = "Error: the folder copy is closed. Send the message again to open a new copy."
_KEY = re.compile(r"[A-Za-z0-9_-]{1,128}")
# The shape of attachment_sandbox.FileLoader; that module imports this one, so the type is named here.
# It takes the attachment ids the session holds, and gives the ids the chat holds now and its other files.
AttachmentLoader = Callable[[set[str]], tuple[set[str], Iterable[Any]]]


@dataclass
class Changes:
    """What changed in the copy since it was made."""

    files: dict[str, str] = field(default_factory=dict)
    deleted: list[str] = field(default_factory=list)
    # Tracked draft paths that were never copied files and are gone from the copy now.
    dropped: list[str] = field(default_factory=list)
    skipped: dict[str, str] = field(default_factory=dict)
    # The listing stopped at its limit, so ``deleted`` and ``dropped`` are not known.
    truncated: bool = False


class _CopyRunner(LocalCommandRunner):
    """Run a setup chat's commands in its folder copy, with the session folder as the sandbox's workspace."""

    def __init__(self, session: Path, folder: Path, egress: LocalEgressProxy) -> None:
        """Make the runner.

        Args:
            session: The chat's session folder, the only folder a command may write.
            folder: The copy inside ``session``, where commands start.
            egress: The session's started egress proxy.
        """
        super().__init__(session, egress)
        self.folder = folder.resolve()

    def _workdir(self, workdir: str | None) -> Path | None:
        """Resolve a working directory inside the copy.

        Args:
            workdir: A path relative to the copy, or None for the copy's root.

        Returns:
            The directory, or None when it leaves the copy.
        """
        path = (self.folder / (workdir or ".")).resolve()
        return path if path == self.folder or self.folder in path.parents else None


def _write_new(root: Path, rel: str, data: bytes, mode: int) -> None:
    """Write a copied file with ``O_EXCL``, walking from ``root`` without following a link.

    Args:
        root: The copy's root.
        rel: The file's ``/``-joined path.
        data: The bytes.
        mode: The permission bits.

    Raises:
        OSError: When a part of the path is a link, or the file exists.
    """
    flags = os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW
    *folders, name = rel.split("/")
    fd = os.open(root, flags)
    try:
        for folder in folders:
            with contextlib.suppress(FileExistsError):
                os.mkdir(folder, dir_fd=fd)
            child = os.open(folder, flags, dir_fd=fd)
            os.close(fd)
            fd = child
        out = os.open(name, os.O_WRONLY | os.O_CREAT | os.O_EXCL | os.O_NOFOLLOW, mode, dir_fd=fd)
        with os.fdopen(out, "wb") as handle:
            handle.write(data)
            os.fchmod(handle.fileno(), mode)
    finally:
        os.close(fd)


def copy_folder(source: Path, folder: Path, entries: list[dict[str, Any]]) -> dict[str, bytes]:
    """Copy the listed files of a folder into an empty folder.

    The source is only read: each file opens with
    :func:`~core.local.folder_tree.open_no_follow`, and a file that became a
    link since the listing is left out.

    Args:
        source: The user's folder.
        folder: The empty copy folder.
        entries: The full :func:`~core.local.folder_tree.folder_entries` listing of ``source``.

    Returns:
        Each copied file's path and its :func:`~core.local.folder_tree.file_digest`
        style digest, computed from the copied bytes.
    """
    digests: dict[str, bytes] = {}
    for entry in entries:
        rel = str(entry["path"])
        if any(is_env_name(part) for part in rel.split("/")):
            continue
        if entry["type"] == "dir":
            with contextlib.suppress(OSError):
                _make_folder(folder, rel)
            continue
        found = _read_source(source, rel)
        if found is None:
            continue
        data, executable = found
        _write_new(folder, rel, data, 0o755 if executable else 0o644)
        digests[rel] = _digest(data, executable)
    return digests


def _read_source(source: Path, rel: str) -> tuple[bytes, bool] | None:
    """Read one file of the user's folder without following a link.

    Args:
        source: The user's folder.
        rel: The file path.

    Returns:
        The bytes and whether the file has an execute bit, or None when the
        file went away, or became a link or a pipe, since the listing.
    """
    try:
        with open_no_follow(source, rel) as handle:
            mode = os.fstat(handle.fileno()).st_mode
            return handle.read(), is_executable(mode)
    except OSError:
        return None


def _digest(data: bytes, executable: bool) -> bytes:
    """Hash file bytes the way :func:`~core.local.folder_tree.file_digest` does.

    Args:
        data: The bytes.
        executable: Whether the file has an execute bit.

    Returns:
        The SHA-1 digest, then :data:`~core.local.folder_tree.EXEC_MARKER` for an executable file.
    """
    digest = hashlib.sha1(data, usedforsecurity=False).digest()
    return digest + EXEC_MARKER if executable else digest


def _make_folder(root: Path, rel: str) -> None:
    """Make one listed folder in the copy, walking without following a link.

    Args:
        root: The copy's root.
        rel: The folder's ``/``-joined path.
    """
    flags = os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW
    fd = os.open(root, flags)
    try:
        for part in rel.split("/"):
            with contextlib.suppress(FileExistsError):
                os.mkdir(part, dir_fd=fd)
            child = os.open(part, flags, dir_fd=fd)
            os.close(fd)
            fd = child
    finally:
        os.close(fd)


class FolderWorkspace:
    """One setup chat's copy of a local folder, its command runner and its egress proxy."""

    def __init__(
        self,
        source: Path,
        session: Path,
        egress_dir: Path,
        secrets: Iterable[SetupSecret] = (),
        attachments: AttachmentLoader | None = None,
    ) -> None:
        """Copy the folder and start the runner.

        Args:
            source: The user's folder.
            session: The chat's session folder; it is emptied first.
            egress_dir: The proxy's state folder, outside ``session``.
            secrets: The saved secrets with hosts, which commands get as placeholders.
            attachments: Loads the chat's attached files, which go next to the copy before each command.

        Raises:
            FolderTreeError: When ``source`` is not a folder.
        """
        self.source = source
        self.session = session
        self.folder = session / COPY_DIR
        self._lock = threading.Lock()
        self.closed = False
        self._attachments = attachments
        self._held: set[str] = set()
        entries, _ = folder_entries(source, limit=None)
        # The same id the folder browser gives, so the drafts' workspace_base compares with it.
        self.base = folder_source_id(source, entries)
        shutil.rmtree(session, ignore_errors=True)
        self.folder.mkdir(parents=True)
        try:
            self.snapshot = copy_folder(source, self.folder, entries)
            try:
                proxy = LocalEgressProxy(list(secrets), egress_dir)
            except ValueError:
                logger.warning("A saved secret does not fit the setup proxy; the session gets no secrets.")
                proxy = LocalEgressProxy([], egress_dir)
            self._egress = proxy.start()
            try:
                self.runner = _CopyRunner(session, self.folder, self._egress)
            except BaseException:
                self._egress.stop()
                raise
        except BaseException:
            shutil.rmtree(session, ignore_errors=True)
            raise

    def source_changed(self) -> bool:
        """Say whether the user's folder changed since the copy was made.

        Returns:
            True when the folder's id differs from :attr:`base`, or the folder is gone.
        """
        try:
            return folder_source_id(self.source) != self.base
        except FolderTreeError:
            return True

    def _scan(self) -> tuple[dict[str, bytes], bool]:
        """List the copy's files with their digests.

        Returns:
            Path to digest, and whether the listing stopped at its limit.
        """
        try:
            entries, truncated = folder_entries(self.folder, FOLDER_ENTRIES_MAX)
        except FolderTreeError:
            return {}, True
        listed: dict[str, bytes] = {}
        for entry in entries:
            if entry["type"] == "file":
                rel = str(entry["path"])
                with contextlib.suppress(OSError):
                    listed[rel] = file_digest(self.folder, rel)
        return listed, truncated

    def _deleted(self, listed: Mapping[str, bytes], truncated: bool) -> list[str]:
        """Find the copied files that are gone from the copy.

        Args:
            listed: The copy's files from :meth:`_scan`.
            truncated: Whether that listing stopped at its limit.

        Returns:
            The paths, with a file that became a folder; a path that is only
            left out of the listing (now ignored, or a link) is not deleted.
        """
        if truncated:
            return []
        return [rel for rel in sorted(set(self.snapshot) - set(listed)) if self._gone(rel)]

    def _gone(self, rel: str) -> bool:
        """Say whether a path of the copy holds no file and no link.

        Args:
            rel: The path.

        Returns:
            True when nothing has the path, or a folder has it. Each part is
            matched against its folder's listing, so a case-only rename on a
            file system that ignores case leaves the old name gone.
        """
        path = self.folder
        for part in rel.split("/"):
            try:
                names = {child.name for child in path.iterdir()}
            except OSError:
                return True
            if part not in names:
                return True
            path = path / part
        return path.is_dir() and not path.is_symlink()

    def reset(self, files: Mapping[str, str], deleted: Iterable[str] = ()) -> None:
        """Make the copy hold the user's folder with the setup draft on top, as the run will see it.

        A change from an earlier turn that is not in the draft now (the user
        discarded it, or it never became a draft) goes back to the folder's file.

        Args:
            files: Path to text: the draft files, ``metric.py`` and ``ascent.toml``.
            deleted: Paths that the draft deletes.
        """
        gone = set(deleted)
        listed, truncated = self._scan()
        for rel, digest in listed.items():
            if rel in files or self.snapshot.get(rel) == digest:
                continue
            if rel in self.snapshot:
                self._restore(rel)
            else:
                with contextlib.suppress(OSError):
                    remove_inside(self.folder, rel)
        for rel in self._deleted(listed, truncated):
            if rel not in gone and rel not in files:
                self._restore(rel)
        # The deletes go first, as in the run, so a draft can swap a file and a folder.
        for path in gone - set(files):
            with contextlib.suppress(ValueError, OSError):
                rel = clean_draft_path(path)
                remove_inside(self.folder, rel)
                # The run keeps no folder that the deletes empty, so a draft file can take its path.
                parent = (self.folder / rel).parent
                while parent != self.folder:
                    parent.rmdir()
                    parent = parent.parent
        for path, text in files.items():
            with contextlib.suppress(ValueError, OSError):
                write_inside(self.folder, clean_draft_path(path), text.encode("utf-8", "surrogatepass"))

    def revert(self, rel: str) -> None:
        """Make one file of the copy match the user's folder again.

        Args:
            rel: The file path; the copy's file is removed when the folder has none.
        """
        found = _read_source(self.source, rel)
        with contextlib.suppress(OSError):
            if found is None:
                remove_inside(self.folder, rel)
            else:
                write_inside(self.folder, rel, found[0], 0o755 if found[1] else 0o644)

    def _restore(self, rel: str) -> None:
        """Copy one file of the user's folder into the copy again.

        Args:
            rel: The file path.
        """
        found = _read_source(self.source, rel)
        if found is not None:
            with contextlib.suppress(OSError):
                write_inside(self.folder, rel, found[0], 0o755 if found[1] else 0o644)

    def source_text(self, rel: str) -> str | None:
        """Read one file of the user's folder as text.

        Args:
            rel: The file path.

        Returns:
            The text, ``""`` when the folder has no such file, or None when
            the file cannot be read as UTF-8 text.
        """
        if not os.path.lexists(self.source / rel):
            return ""
        found = _read_source(self.source, rel)
        if found is None or len(found[0]) > DRAFT_FILE_BYTES:
            return None
        try:
            return found[0].decode("utf-8")
        except UnicodeDecodeError:
            return None

    def write(self, path: str, text: str) -> None:
        """Write one file into the copy, for the agent's write and edit tools.

        Args:
            path: The clean path.
            text: The whole text.
        """
        with contextlib.suppress(OSError):
            write_inside(self.folder, path, text.encode("utf-8", "surrogatepass"))

    def changes(self, tracked: Iterable[str] = ()) -> Changes:
        """Compare the copy with the folder as it was copied.

        Args:
            tracked: Draft paths whose current text is returned even when it
                matches the folder again, so a reverted draft goes back to the
                folder's text.

        Returns:
            The changed and tracked files as text, the deleted copied files,
            the tracked paths that are gone, and the changed paths that cannot
            be drafts, each with its reason. A truncated listing finds no
            deleted or gone path.
        """
        result = Changes()
        listed, truncated = self._scan()
        result.truncated = truncated
        if truncated:
            result.skipped["*"] = f"The copy has more than {FOLDER_ENTRIES_MAX} entries; later files were not checked."
        wanted = {rel for rel, digest in listed.items() if self.snapshot.get(rel) != digest}
        wanted |= {rel for rel in tracked if rel in listed}
        for rel in sorted(wanted):
            if reason := self._text(rel, result):
                result.skipped[rel] = reason
        result.deleted = self._deleted(listed, truncated)
        if not truncated:
            result.dropped = sorted(rel for rel in set(tracked) - set(listed) - set(self.snapshot) if self._gone(rel))
        return result

    def _text(self, rel: str, result: Changes) -> str:
        """Read one changed file of the copy into ``result.files``.

        Args:
            rel: The file path.
            result: The changes to fill.

        Returns:
            The reason the file cannot be a draft, or ``""``.
        """
        try:
            clean_draft_path(rel)
        except ValueError as error:
            return str(error)
        try:
            with open_no_follow(self.folder, rel) as handle:
                data = handle.read(DRAFT_FILE_BYTES + 1)
        except OSError:
            return "The file cannot be read."
        if len(data) > DRAFT_FILE_BYTES:
            return f"The file is larger than {DRAFT_FILE_BYTES} bytes."
        try:
            result.files[rel] = data.decode("utf-8")
        except UnicodeDecodeError:
            return "The file is not UTF-8 text."
        return ""

    def exec_command(self, *args: Any) -> str:
        """Run a command in the copy.

        Args:
            args: The :meth:`.LocalCommandRunner.exec_command` arguments.

        Returns:
            The result text for the model.
        """
        with self._lock:
            if self.closed:
                return CLOSED
            self._sync_attachments()
        return self.runner.exec_command(*args)

    def _sync_attachments(self) -> None:
        """Write the chat's new attached files next to the copy, and remove the ones that left the chat."""
        if self._attachments is None:
            return
        try:
            held, new = self._attachments(set(self._held))
            root = self.session.resolve()
            for media_id in self._held - held:
                gone = (self.session / ATTACHMENTS_DIR / media_id).resolve()
                # A command can make ``attachments`` a link; never delete outside the session.
                if root in gone.parents:
                    shutil.rmtree(gone, ignore_errors=True)
            self._held &= held
            for file in new:
                name = "file" if file.name in ("", ".", "..") else file.name
                try:
                    write_inside(self.session, f"{ATTACHMENTS_DIR}/{file.id}/{name}", file.data, 0o600)
                except OSError:
                    logger.warning("A chat file cannot be written next to the setup folder copy.")
                    continue
                self._held.add(file.id)
        except Exception:  # the command still runs, without the newer files
            logger.exception("The chat files cannot load for the setup folder copy")

    def write_stdin(self, *args: Any) -> str:
        """Send input to a running command, or read its newer output.

        Args:
            args: The :meth:`.LocalCommandRunner.write_stdin` arguments.

        Returns:
            The result text for the model.
        """
        if self.closed:
            return CLOSED
        return self.runner.write_stdin(*args)

    def apply_patch(self, patch: str) -> str:
        """Apply one patch to the copy.

        Args:
            patch: The patch text.

        Returns:
            One line for each changed file.

        Raises:
            PatchError: For a patch that changes nothing.
            OSError: When a write fails part way.
        """
        with self._lock:
            if self.closed:
                return CLOSED
            return "\n".join(apply_patch(self.folder, patch))

    def stop_commands(self) -> None:
        """End every running command of the chat, for the Stop button."""
        self.runner.close()

    def close(self) -> None:
        """End the commands, stop the proxy and delete the copy."""
        with self._lock:
            self.closed = True
        self.runner.close()
        self._egress.stop()
        shutil.rmtree(self.session, ignore_errors=True)


_WORKSPACES: dict[str, FolderWorkspace] = {}
_LOCK = threading.Lock()
# One lock for each chat, so a slow copy of one folder never holds up another chat.
_KEY_LOCKS: dict[str, threading.Lock] = {}
# Chats closed while their copy was made; that copy is thrown away, not kept.
_ABORTED: set[str] = set()


def _state_root() -> Path:
    """Return the app's state folder.

    Returns:
        ``settings.local_state_dir``, or the temporary folder when it is not set.
    """
    return Path(settings.local_state_dir) if settings.local_state_dir else Path(tempfile.gettempdir())


def session_folder(key: str) -> Path:
    """Return the session folder of a setup chat.

    Args:
        key: The chat id.

    Returns:
        The folder path.

    Raises:
        ValueError: For a chat id that is not a safe folder name.
    """
    if not _KEY.fullmatch(key):
        raise ValueError("The chat id is not a safe folder name.")
    return _state_root() / SESSIONS_DIR / key


def sweep_stale_copies() -> None:
    """Delete the folder copies and proxy state folders that an earlier app process left.

    The app's start calls this before any setup chat opens a copy.
    """
    root = _state_root()
    shutil.rmtree(root / SESSIONS_DIR, ignore_errors=True)
    egress = root / EGRESS_DIR
    if egress.is_dir() and not egress.is_symlink():
        for stale in egress.glob("egress-*"):
            shutil.rmtree(stale, ignore_errors=True)


def open_workspace(
    key: str,
    source: str | os.PathLike[str],
    secrets: Callable[[], list[SetupSecret]] | None = None,
    attachments: AttachmentLoader | None = None,
) -> tuple[FolderWorkspace, bool]:
    """Return the chat's folder copy, and make it on first use or when the folder changed.

    Args:
        key: The chat id.
        source: The user's folder.
        secrets: Returns the saved secrets with hosts, called only for a new copy.
        attachments: Loads the chat's attached files, for a new copy.

    Returns:
        The workspace, and whether it is new.

    Raises:
        FolderTreeError: When ``source`` is not a folder.
        ValueError: For a chat id that is not a safe folder name, or a chat
            that closed while its copy was made.
    """
    session = session_folder(key)
    folder = Path(source)
    with _LOCK:
        key_lock = _KEY_LOCKS.setdefault(key, threading.Lock())
    with key_lock:
        with _LOCK:
            _ABORTED.discard(key)
            found = _WORKSPACES.get(key)
        if found is not None and not found.closed and found.source == folder and not found.source_changed():
            return found, False
        if found is not None:
            with _LOCK:
                if _WORKSPACES.get(key) is found:
                    del _WORKSPACES[key]
            found.close()
        workspace = FolderWorkspace(
            folder, session, _state_root() / EGRESS_DIR, secrets() if secrets else (), attachments
        )
        with _LOCK:
            aborted = key in _ABORTED
            _ABORTED.discard(key)
            if not aborted:
                _WORKSPACES[key] = workspace
        if aborted:
            workspace.close()
            raise ValueError("The chat closed while its folder was copied.")
        return workspace, True


def live_workspace(key: str) -> FolderWorkspace | None:
    """Return the chat's open folder copy, if it has one.

    Args:
        key: The chat id.

    Returns:
        The workspace, or None.
    """
    with _LOCK:
        return _WORKSPACES.get(key)


def close_workspace(key: str) -> None:
    """Delete a chat's folder copy, when the chat is deleted or its run is submitted.

    Args:
        key: The chat id.
    """
    with _LOCK:
        workspace = _WORKSPACES.pop(key, None)
        key_lock = _KEY_LOCKS.get(key)
        copying = key_lock is not None and key_lock.locked()
        if copying:
            _ABORTED.add(key)
    try:
        if workspace is not None:
            workspace.close()
        elif settings.local_mode and not copying:
            # A copy in progress deletes its own folder when it sees the abort.
            shutil.rmtree(session_folder(key), ignore_errors=True)
    except Exception:
        logger.exception("A setup folder copy failed to close")


def saved_setup_secrets(engine: Any, username: str) -> list[SetupSecret]:
    """Load the user's saved secrets that have hosts, for the setup proxy's placeholders.

    A secret without hosts would enter the command environment as plain
    text, and a command can send it anywhere, so it stays out.

    Args:
        engine: The SQLAlchemy engine of ``user_secrets``.
        username: The secrets' owner.

    Returns:
        The secrets; a secret that fails to decrypt is left out, and a failed list gives none.
    """
    store = SavedSecretStore(engine)
    try:
        views = store.list(username)
    except Exception:  # the shell still runs, with no saved secrets
        logger.info("The saved secrets cannot load for the setup proxy.")
        return []
    found: list[SetupSecret] = []
    for view in views:
        if not view.allowed_hosts:
            continue
        try:
            value = store.resolve(username, view.id, view.allowed_hosts)
        except Exception:  # one bad secret must not close the shell
            logger.info("A saved secret cannot load for the setup proxy.")
            continue
        found.append(SetupSecret(view.name, value, tuple(view.allowed_hosts)))
    return found


__all__ = [
    "COPY_DIR",
    "SESSIONS_DIR",
    "AttachmentLoader",
    "Changes",
    "FolderWorkspace",
    "close_workspace",
    "copy_folder",
    "live_workspace",
    "open_workspace",
    "saved_setup_secrets",
    "session_folder",
    "sweep_stale_copies",
]
