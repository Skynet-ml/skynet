"""Commit each harness command as one agent step, with the workspace tree after it (SKY-715).

Two sources name a command. A model call through the mailbox: each
``tool_result`` in the newest turn of an Anthropic Messages request answers a
``tool_use`` of an earlier assistant turn, and the pair is one command. The
exec shim: a command that no model call names is one line of the session's
exec log (``exec_shim``). :meth:`HarnessCapture.observe` takes each mailbox
request, before it goes to the model, and commits the commands that ran since
the previous one, with the tree of that moment.

Step contract: a command commits once. When a request answers new tool calls,
only the ``tool_use`` steps commit, and every exec log line of the same
interval is dropped. Most of those lines ran inside the tool calls (a model's
Bash tool that runs a listed command). A command the harness ran on its own in
that interval is dropped too and never becomes a step: a known gap. Exec log
lines commit only for an interval with no new tool result.

The exec log is per session, and a session can serve many captures (one per
turn). A capture reads only the lines written after it starts.

The route key reaches the harness env, so tool output, exec records and
workspace files can hold it. Every exact copy becomes ``KEY_TOKEN`` before a
step body or blob is stored.

The committed tree is the workspace with the bridge's session folder under
``SESSIONS_DIR/<session id>``, so a resume in a new box can restore the
harness's own session state (:func:`restore_session`) before it starts.
"""

from __future__ import annotations

import json
import os
import shutil
import stat
from collections.abc import Callable, Sequence
from fnmatch import fnmatchcase
from pathlib import Path
from typing import Any

from ....storage.step_log_store import StepLogStore
from ....storage.tree_store import TreeEntry, TreeStore
from .exec_shim import OUTPUT_LIMIT
from .harness_bridge import EXEC_LOG, EXEC_SHIM_DIR, KEY_TOKEN, SESSIONS_DIR, workspace_files

# Session-folder files that are only logs of this box: the raw harness output
# and the exec log can hold the route key, and a restored exec log would commit
# its old records again.
_SESSION_LOGS = ("run-*.stdout", "run-*.stderr", EXEC_LOG, EXEC_SHIM_DIR)
# The agent can write the exec log without limit; one read takes at most this much.
_EXEC_READ_LIMIT = 8 << 20
# Deeper folders are left out of a snapshot: the walk recurses and holds one descriptor per level.
_MAX_DEPTH = 64


class HarnessCapture:
    """Turn one agent's mailbox requests and exec log into command steps, in order."""

    def __init__(
        self,
        step_log: StepLogStore,
        *,
        run_id: str,
        agent_id: str,
        execution_generation: int,
        grant_list_id: str,
        snapshot: Callable[[], str],
        route_key: str,
        exec_log: Path | None = None,
        seq: int = 0,
    ) -> None:
        """Bind the capture to one agent's step log position.

        Args:
            step_log: The worker's step log.
            run_id: The job the steps belong to.
            agent_id: The agent that commits them.
            execution_generation: The run generation the agent works under.
            grant_list_id: The grant list the commands ran with.
            snapshot: Store the current workspace and session folder and
                return the tree id (for example :func:`snapshot_session`).
            route_key: The key the harness routes with; step bodies never hold it.
            exec_log: The session's exec shim log, when the launch records
                commands. The capture reads only what is written after it starts.
            seq: The agent's next step position.
        """
        self._step_log = step_log
        self._run_id = run_id
        self._agent_id = agent_id
        self._generation = execution_generation
        self._grant_list_id = grant_list_id
        self._snapshot = snapshot
        self._route_key = route_key
        self._exec_log = exec_log
        # Earlier turns of the session wrote the lines before this point, and their captures committed them.
        self._exec_offset = 0
        opened = self._open_exec_log()
        if opened is not None:
            os.close(opened[0])
            self._exec_offset = opened[1]
        self._seq = seq
        # A harness resends a request when the model call fails; its pairs are already steps.
        self._committed: set[str] = set()

    def observe(self, request: dict[str, Any]) -> list[str]:
        """Commit the tool calls the request answers, or else the exec log's new commands.

        Args:
            request: One Anthropic Messages request body from the mailbox.

        Returns:
            The new step ids, in commit order.
        """
        pairs = [(body, None) for body in _tool_pairs(request) if body["tool_use_id"] not in self._committed]
        if not pairs:
            return self._drain(commit=True)
        step_ids = self._commit(pairs)
        self._drain(commit=False)
        return step_ids

    def finish(self) -> list[str]:
        """Commit the exec log's commands that ran after the last model call.

        Returns:
            The new step ids, in commit order.
        """
        return self._drain(commit=True)

    def _drain(self, *, commit: bool) -> list[str]:
        """Read the exec log up to its size at the start, one limited read at a time, and commit or drop its records.

        A child can keep appending to the log, so lines written during the
        drain wait for the next call; otherwise the drain might never end.

        Args:
            commit: Commit the records; ``False`` drops them (a tool call interval).

        Returns:
            The new step ids, in commit order.
        """
        opened = self._open_exec_log()
        if opened is None:
            return []
        os.close(opened[0])
        stop = opened[1]
        step_ids: list[str] = []
        while True:
            start = self._exec_offset
            records, offset = self._exec_records()
            if commit:
                step_ids += self._commit(records)
            self._exec_offset = offset
            if offset == start or offset >= stop:
                return step_ids

    def _commit(self, items: Sequence[tuple[dict[str, Any], int | None]]) -> list[str]:
        """Commit each body as a command step, all with one tree.

        The capture records each step right after its commit (its tool call
        id, or its exec log position), so a retry after a failed commit does
        not commit the earlier ones again.

        Args:
            items: Step bodies, in the order the commands ran, each with the
                exec log position after its line (``None`` for a tool call).

        Returns:
            The step ids.
        """
        if not items:
            return []
        tree_id = self._snapshot()
        step_ids = []
        for body, end in items:
            try:
                redacted = _redact(body, self._route_key)
            # The model request is agent-controlled. A call whose input is too
            # deep to redact still ran, so it commits with no input.
            except RecursionError:
                redacted = _redact(
                    {
                        "source": body["source"],
                        "tool_use_id": body["tool_use_id"],
                        "tool": body["tool"],
                        "input_too_deep": True,
                    },
                    self._route_key,
                )
            step_ids.append(
                self._step_log.commit(
                    run_id=self._run_id,
                    agent_id=self._agent_id,
                    seq=self._seq,
                    kind="command",
                    execution_generation=self._generation,
                    grant_list_id=self._grant_list_id,
                    body=redacted,
                    tree_id=tree_id,
                )
            )
            self._seq += 1
            if end is None:
                self._committed.add(body["tool_use_id"])
            else:
                self._exec_offset = end
        return step_ids

    def _exec_records(self) -> tuple[list[tuple[dict[str, Any], int]], int]:
        """Read the exec log's complete new lines as step bodies.

        The agent can write the log, so a line that is not a well-formed
        record is skipped. A line with no newline yet is read next time.
        The caller moves the read position only after the commit, so a failed
        snapshot reads the same lines again. One call reads at most
        ``_EXEC_READ_LIMIT`` bytes; a full read with no newline is skipped
        whole, so one huge line cannot stop the log.

        Returns:
            One body for each well-formed new record, with the log position
            after its line, and the log position after all read lines.
        """
        opened = self._open_exec_log()
        if opened is None:
            return [], self._exec_offset
        fd = opened[0]
        try:
            os.lseek(fd, self._exec_offset, os.SEEK_SET)
            chunks: list[bytes] = []
            size = 0
            while size < _EXEC_READ_LIMIT and (chunk := os.read(fd, _EXEC_READ_LIMIT - size)):
                chunks.append(chunk)
                size += len(chunk)
        finally:
            os.close(fd)
        data = b"".join(chunks)
        complete = data[: data.rfind(b"\n") + 1]
        if not complete and size >= _EXEC_READ_LIMIT:
            return [], self._exec_offset + size
        offset = self._exec_offset + len(complete)
        bodies = []
        end = self._exec_offset
        for line in complete.splitlines(keepends=True):
            end += len(line)
            try:
                record = json.loads(line)
            # Deeply nested brackets make the parser raise RecursionError, not ValueError.
            except (ValueError, RecursionError):
                continue
            if (
                isinstance(record, dict)
                and isinstance(record.get("command"), str)
                and type(record.get("exit_code")) is int
                and isinstance(record.get("stdout"), str)
                and isinstance(record.get("stderr"), str)
            ):
                body = {"source": "exec_shim", "command": record["command"], "exit_code": record["exit_code"]}
                for stream in ("stdout", "stderr"):
                    body[stream] = _drop_cut_key(record[stream], self._route_key)
                bodies.append((body, end))
        return bodies, offset

    def _open_exec_log(self) -> tuple[int, int] | None:
        """Open the exec log for reading, only when it is a regular file.

        The agent can replace the log with a link (to ``/dev/zero`` or a host
        file) or a pipe, so the open does not follow a link or wait for a writer.

        Returns:
            The open descriptor, which the caller closes, and the log size; or
            ``None`` when there is no log or it is not a regular file.
        """
        if self._exec_log is None:
            return None
        try:
            fd = os.open(self._exec_log, os.O_RDONLY | os.O_NOFOLLOW | os.O_NONBLOCK)
        except OSError:
            return None
        info = os.fstat(fd)
        if not stat.S_ISREG(info.st_mode):
            os.close(fd)
            return None
        return fd, info.st_size


def _drop_cut_key(text: str, route_key: str) -> str:
    """Remove the start of an exec log stream that the shim cut to its tail.

    The shim keeps only the last ``OUTPUT_LIMIT`` bytes, so the cut can split
    a copy of the route key, and ``_redact`` replaces only whole copies. A
    split copy leaves fewer than ``len(route_key)`` characters at the start of
    the tail, so those are removed. A stream shorter than the limit is whole.

    Args:
        text: The decoded stream tail.
        route_key: The key; an empty key removes nothing.

    Returns:
        The tail, without its first ``len(route_key) - 1`` characters when it may be cut.
    """
    if not route_key or len(text.encode("utf-8")) < OUTPUT_LIMIT:
        return text
    return text[len(route_key) - 1 :]


def _redact(value: Any, route_key: str) -> Any:
    """Replace every exact copy of the route key in a JSON value with ``KEY_TOKEN``.

    Args:
        value: A step body or a part of it.
        route_key: The key; an empty key replaces nothing.

    Returns:
        The value with each string, object key included, redacted.
    """
    if not route_key:
        return value
    if isinstance(value, str):
        return value.replace(route_key, KEY_TOKEN)
    if isinstance(value, dict):
        return {_redact(key, route_key): _redact(item, route_key) for key, item in value.items()}
    if isinstance(value, list):
        return [_redact(item, route_key) for item in value]
    return value


def _tool_pairs(request: dict[str, Any]) -> list[dict[str, Any]]:
    """Pair each ``tool_result`` of the request's newest turn with its ``tool_use``.

    Args:
        request: An Anthropic Messages request body.

    Returns:
        One step body for each answered call, in result order. The request is
        agent-controlled, so a call or result whose id is not a non-empty
        string is left out.
    """
    messages = request.get("messages")
    if not isinstance(messages, list) or not messages or not isinstance(messages[-1], dict):
        return []
    uses = {
        block["id"]: block
        for message in messages
        if isinstance(message, dict) and message.get("role") == "assistant"
        for block in _blocks(message)
        if block.get("type") == "tool_use" and _is_id(block.get("id"))
    }
    pairs = []
    for block in _blocks(messages[-1]):
        if block.get("type") != "tool_result" or not _is_id(block.get("tool_use_id")):
            continue
        use = uses.get(block["tool_use_id"])
        if use is None:
            continue
        pairs.append(
            {
                "source": "tool_use",
                "tool_use_id": use["id"],
                "tool": str(use.get("name") or ""),
                "command": use.get("input"),
                "output": _result_text(block.get("content")),
                "is_error": bool(block.get("is_error")),
            }
        )
    return pairs


def _is_id(value: Any) -> bool:
    """Tell whether a block id is a non-empty string.

    Args:
        value: The ``id`` or ``tool_use_id`` value.

    Returns:
        Whether it can pair a call with its result.
    """
    return isinstance(value, str) and bool(value)


def _blocks(message: dict[str, Any]) -> list[dict[str, Any]]:
    """Return the content blocks of one message.

    Args:
        message: One request message.

    Returns:
        Its block objects; a plain-text message has none.
    """
    content = message.get("content")
    return [block for block in content if isinstance(block, dict)] if isinstance(content, list) else []


def _result_text(content: Any) -> str:
    """Return the text of a ``tool_result`` content.

    Args:
        content: A string or a list of content blocks.

    Returns:
        The text, with the text blocks joined by newlines.
    """
    if isinstance(content, str):
        return content
    if not isinstance(content, list):
        return ""
    return "\n".join(
        str(block.get("text") or "") for block in content if isinstance(block, dict) and block.get("type") == "text"
    )


def snapshot_session(
    trees: TreeStore,
    run_id: str,
    workspace: Path,
    session_dir: Path,
    *,
    proposer: dict[str, Any],
    route_key: str,
) -> str:
    """Store the workspace and the session folder as one tree, with no copy of the route key.

    Left out: the launch files (``harness_bridge.workspace_files``), which hold
    the key; a workspace entry named ``SESSIONS_DIR``, whose slot holds the
    real session folder; the session folder's logs (``_SESSION_LOGS``); and
    files that cannot be read.

    Args:
        trees: The worker's tree store.
        run_id: The job that owns the objects.
        workspace: The agent's workspace.
        session_dir: The bridge's session folder, ``<home>/SESSIONS_DIR/<session id>``.
        proposer: The serialized launch that wrote the launch files.
        route_key: The key the harness routes with.

    Returns:
        The root tree id.
    """
    launch_files = {*workspace_files(proposer), SESSIONS_DIR}
    entries = _put_root(trees, run_id, workspace, launch_files.__contains__, route_key)

    def is_log(relative: str) -> bool:
        """Tell whether a session-folder path is a log of this box.

        Args:
            relative: The session-folder-relative POSIX path.

        Returns:
            Whether it matches ``_SESSION_LOGS``.
        """
        return any(fnmatchcase(relative, pattern) for pattern in _SESSION_LOGS)

    session = trees.put_tree(run_id, _put_root(trees, run_id, session_dir, is_log, route_key))
    sessions = trees.put_tree(run_id, [TreeEntry(session_dir.name, "tree", session)])
    return trees.put_tree(run_id, [*entries, TreeEntry(SESSIONS_DIR, "tree", sessions)])


def _put_root(
    trees: TreeStore, run_id: str, folder: Path, skip: Callable[[str], bool], route_key: str
) -> list[TreeEntry]:
    """Store everything below one root folder, which must not be a link.

    Args:
        trees: The tree store.
        run_id: The job that owns the objects.
        folder: The root folder.
        skip: Tell whether a root-relative POSIX path is left out.
        route_key: The key to redact.

    Returns:
        The folder's tree entries.
    """
    folder_fd = os.open(folder, os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW)
    try:
        return _put_directory(trees, run_id, folder_fd, "", skip, route_key, 0)
    finally:
        os.close(folder_fd)


def _put_directory(
    trees: TreeStore,
    run_id: str,
    folder_fd: int,
    prefix: str,
    skip: Callable[[str], bool],
    route_key: str,
    depth: int,
) -> list[TreeEntry]:
    """Store every file, link and folder in an open folder and return its entries.

    The agent can swap an entry for a link at any moment, so every read goes
    through a descriptor opened relative to ``folder_fd`` with ``O_NOFOLLOW``.
    ``O_NONBLOCK`` keeps a pipe from hanging the open. An entry whose open
    fails, or whose opened file is not the one ``lstat`` saw, is left out.

    Args:
        trees: The tree store.
        run_id: The job that owns the objects.
        folder_fd: An open descriptor of the folder.
        prefix: The folder's root-relative POSIX path, with a trailing ``/``.
        skip: Tell whether a root-relative POSIX path is left out.
        route_key: Each copy in a blob becomes ``KEY_TOKEN``; a name that holds it is left out.
        depth: The folder's depth below the root; its subfolders are left
            out at ``_MAX_DEPTH``.

    Returns:
        The folder's tree entries. Sockets, pipes and devices are left out.
    """
    entries = []
    for name in sorted(os.listdir(folder_fd)):
        if skip(prefix + name) or (route_key and route_key in name) or not _is_utf8(name):
            continue
        # A background job can delete a file during the walk, and the agent can
        # make a file unreadable; the tree then holds what can still be read.
        try:
            info = os.stat(name, dir_fd=folder_fd, follow_symlinks=False)
            if stat.S_ISLNK(info.st_mode):
                # readlink reads the link itself and never follows it.
                target = _redact_bytes(os.fsencode(os.readlink(name, dir_fd=folder_fd)), route_key)
                entries.append(TreeEntry(name, "symlink", trees.put_blob(run_id, target)))
            elif (stat.S_ISDIR(info.st_mode) and depth < _MAX_DEPTH) or stat.S_ISREG(info.st_mode):
                entry = _put_opened(trees, run_id, folder_fd, prefix, name, info, skip, route_key, depth)
                if entry is not None:
                    entries.append(entry)
        except OSError:
            continue
    return entries


def _is_utf8(name: str) -> bool:
    """Tell whether a file name can be stored in a tree.

    ``os.listdir`` returns a name that is not valid UTF-8 with surrogate
    escapes, and ``encode_tree`` cannot encode it.

    Args:
        name: The entry name.

    Returns:
        Whether the name encodes as UTF-8.
    """
    try:
        name.encode("utf-8")
    except UnicodeEncodeError:
        return False
    return True


def _put_opened(
    trees: TreeStore,
    run_id: str,
    folder_fd: int,
    prefix: str,
    name: str,
    info: os.stat_result,
    skip: Callable[[str], bool],
    route_key: str,
    depth: int,
) -> TreeEntry | None:
    """Open one file or folder without following a link and store it, if it is still what ``lstat`` saw.

    Args:
        trees: The tree store.
        run_id: The job that owns the objects.
        folder_fd: The open parent folder.
        prefix: The parent's root-relative POSIX path.
        name: The entry name.
        info: The entry's ``lstat`` result.
        skip: Tell whether a root-relative POSIX path is left out.
        route_key: The key to redact.
        depth: The parent's depth below the root.

    Returns:
        The entry, or ``None`` when the opened file is another file or type.

    Raises:
        OSError: When the open fails, for example ``ELOOP`` on a link.
    """
    fd = os.open(name, os.O_RDONLY | os.O_NOFOLLOW | os.O_NONBLOCK, dir_fd=folder_fd)
    try:
        opened = os.fstat(fd)
        if (opened.st_dev, opened.st_ino, stat.S_IFMT(opened.st_mode)) != (
            info.st_dev,
            info.st_ino,
            stat.S_IFMT(info.st_mode),
        ):
            return None
        if stat.S_ISDIR(opened.st_mode):
            subtree = trees.put_tree(
                run_id, _put_directory(trees, run_id, fd, f"{prefix}{name}/", skip, route_key, depth + 1)
            )
            return TreeEntry(name, "tree", subtree)
        chunks = []
        while chunk := os.read(fd, 1 << 20):
            chunks.append(chunk)
        mode = "exec" if opened.st_mode & 0o111 else "file"
        return TreeEntry(name, mode, trees.put_blob(run_id, _redact_bytes(b"".join(chunks), route_key)))
    finally:
        os.close(fd)


def _redact_bytes(data: bytes, route_key: str) -> bytes:
    """Replace every exact copy of the route key in file bytes with ``KEY_TOKEN``.

    Args:
        data: The bytes.
        route_key: The key; an empty key replaces nothing.

    Returns:
        The redacted bytes.
    """
    return data.replace(route_key.encode(), KEY_TOKEN.encode()) if route_key else data


def restore_session(trees: TreeStore, run_id: str, tree_id: str, workspace: Path, home: Path) -> None:
    """Write a :func:`snapshot_session` tree back: the workspace, and the session folder under ``home``.

    The workspace and each restored folder end up holding only what the tree
    names, so a file the agent deleted does not come back. ``home/SESSIONS_DIR``
    keeps its other sessions, and the workspace keeps its own ``SESSIONS_DIR``.

    Args:
        trees: The tree store.
        run_id: The job that reads the objects.
        tree_id: The root tree id of a committed step.
        workspace: The agent's workspace in the new box.
        home: The home folder in the new box; the bridge reads ``home/SESSIONS_DIR``.

    Raises:
        ValueError: When an object is missing or an entry name is not a plain name.
    """
    entries = _entries(trees, run_id, tree_id)
    workspace.mkdir(parents=True, exist_ok=True)
    _prune(workspace, {entry.name for entry in entries})
    for entry in entries:
        if entry.name != SESSIONS_DIR:
            _restore(trees, run_id, entry, workspace / entry.name)
        elif entry.mode == "tree":
            sessions = home / SESSIONS_DIR
            if sessions.is_symlink() or (sessions.exists() and not sessions.is_dir()):
                sessions.unlink()
            sessions.mkdir(parents=True, exist_ok=True)
            for session in _entries(trees, run_id, entry.object_id):
                _restore(trees, run_id, session, sessions / session.name)


def _prune(folder: Path, keep: set[str]) -> None:
    """Delete the entries of ``folder`` that ``keep`` does not name, without following a link.

    Args:
        folder: A real folder.
        keep: The names to keep.
    """
    for path in folder.iterdir():
        if path.name in keep:
            continue
        if path.is_dir() and not path.is_symlink():
            shutil.rmtree(path)
        else:
            path.unlink()


def _entries(trees: TreeStore, run_id: str, tree_id: str) -> list[TreeEntry]:
    """Read a tree whose entries are safe to write as paths.

    Args:
        trees: The tree store.
        run_id: The job that reads the tree.
        tree_id: The tree id.

    Returns:
        The entries.

    Raises:
        ValueError: When the tree is missing or an entry name could leave its folder.
    """
    entries = trees.get_tree(run_id, tree_id)
    if entries is None:
        raise ValueError(f"tree object {tree_id} is missing")
    for entry in entries:
        if entry.name in ("", ".", "..") or "/" in entry.name or "\0" in entry.name:
            raise ValueError(f"bad tree entry name: {entry.name!r}")
    return entries


def _restore(trees: TreeStore, run_id: str, entry: TreeEntry, target: Path) -> None:
    """Write one entry at ``target``, replacing what is there without following a link.

    Args:
        trees: The tree store.
        run_id: The job that reads the objects.
        entry: The tree entry.
        target: Where it goes.

    Raises:
        ValueError: When an object is missing.
    """
    is_folder = target.is_dir() and not target.is_symlink()
    if entry.mode == "tree":
        if target.is_symlink() or (target.exists() and not is_folder):
            target.unlink()
        target.mkdir(parents=True, exist_ok=True)
        children = _entries(trees, run_id, entry.object_id)
        _prune(target, {child.name for child in children})
        for child in children:
            _restore(trees, run_id, child, target / child.name)
        return
    data = trees.get_blob(run_id, entry.object_id)
    if data is None:
        raise ValueError(f"blob object {entry.object_id} is missing")
    if is_folder:
        shutil.rmtree(target)
    elif target.is_symlink() or target.exists():
        # Unlink first: writing into an existing link or hard link would change a file outside the tree.
        target.unlink()
    target.parent.mkdir(parents=True, exist_ok=True)
    if entry.mode == "symlink":
        target.symlink_to(os.fsdecode(data))
        return
    target.write_bytes(data)
    target.chmod(0o755 if entry.mode == "exec" else 0o644)
