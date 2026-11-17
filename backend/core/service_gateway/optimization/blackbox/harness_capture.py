"""Commit each harness command as one agent step, with the workspace tree after it (SKY-715).

Two sources name a command. A model call through the mailbox: each
``tool_result`` in the newest turn of an Anthropic Messages request answers a
``tool_use`` of an earlier assistant turn, and the pair is one command. The
exec shim: a command that no model call names is one line of the session's
exec log (``exec_shim``). :meth:`HarnessCapture.observe` takes each mailbox
request, before it goes to the model, and commits the commands that ran since
the previous one, with the tree of that moment.

The committed tree is the workspace with the bridge's session folder under
``SESSIONS_DIR/<session id>``, so a resume in a new box restores the
harness's own session state (:func:`restore_session`) before it starts.
"""

from __future__ import annotations

import json
import os
import shutil
import stat
from collections.abc import Callable, Iterable
from pathlib import Path
from typing import Any

from ....storage.step_log_store import StepLogStore
from ....storage.tree_store import TreeEntry, TreeStore
from .harness_bridge import SESSIONS_DIR


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
            exec_log: The session's exec shim log, when the launch records commands.
            seq: The agent's next step position.
        """
        self._step_log = step_log
        self._run_id = run_id
        self._agent_id = agent_id
        self._generation = execution_generation
        self._grant_list_id = grant_list_id
        self._snapshot = snapshot
        self._exec_log = exec_log
        self._exec_offset = 0
        self._seq = seq
        # A harness resends a request when the model call fails; its pairs are already steps.
        self._committed: set[str] = set()

    def observe(self, request: dict[str, Any]) -> list[str]:
        """Commit the exec log's new commands and the tool calls the request answers.

        Args:
            request: One Anthropic Messages request body from the mailbox.

        Returns:
            The new step ids, in commit order.
        """
        bodies = self._exec_records()
        for body in _tool_pairs(request):
            if body["tool_use_id"] not in self._committed:
                bodies.append(body)
        step_ids = self._commit(bodies)
        self._committed.update(body["tool_use_id"] for body in bodies if body["source"] == "tool_use")
        return step_ids

    def finish(self) -> list[str]:
        """Commit the exec log's commands that ran after the last model call.

        Returns:
            The new step ids, in commit order.
        """
        return self._commit(self._exec_records())

    def _commit(self, bodies: list[dict[str, Any]]) -> list[str]:
        """Commit each body as a command step, all with one tree.

        Args:
            bodies: Step bodies, in the order the commands ran.

        Returns:
            The step ids.
        """
        if not bodies:
            return []
        tree_id = self._snapshot()
        step_ids = []
        for body in bodies:
            step_ids.append(
                self._step_log.commit(
                    run_id=self._run_id,
                    agent_id=self._agent_id,
                    seq=self._seq,
                    kind="command",
                    execution_generation=self._generation,
                    grant_list_id=self._grant_list_id,
                    body=body,
                    tree_id=tree_id,
                )
            )
            self._seq += 1
        return step_ids

    def _exec_records(self) -> list[dict[str, Any]]:
        """Read the exec log's complete new lines as step bodies.

        The agent can write the log, so a line that is not a well-formed
        record is skipped. A line with no newline yet is read next time.

        Returns:
            One body for each well-formed new record.
        """
        if self._exec_log is None or not self._exec_log.exists():
            return []
        with self._exec_log.open("rb") as handle:
            handle.seek(self._exec_offset)
            data = handle.read()
        complete = data[: data.rfind(b"\n") + 1]
        self._exec_offset += len(complete)
        bodies = []
        for line in complete.splitlines():
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
                bodies.append(
                    {
                        "source": "exec_shim",
                        **{key: record[key] for key in ("command", "exit_code", "stdout", "stderr")},
                    }
                )
        return bodies


def _tool_pairs(request: dict[str, Any]) -> list[dict[str, Any]]:
    """Pair each ``tool_result`` of the request's newest turn with its ``tool_use``.

    Args:
        request: An Anthropic Messages request body.

    Returns:
        One step body for each answered call, in result order.
    """
    messages = request.get("messages")
    if not isinstance(messages, list) or not messages or not isinstance(messages[-1], dict):
        return []
    uses = {
        block.get("id"): block
        for message in messages
        if isinstance(message, dict) and message.get("role") == "assistant"
        for block in _blocks(message)
        if block.get("type") == "tool_use"
    }
    pairs = []
    for block in _blocks(messages[-1]):
        use = uses.get(block.get("tool_use_id")) if block.get("type") == "tool_result" else None
        if use is None:
            continue
        pairs.append(
            {
                "source": "tool_use",
                "tool_use_id": str(use["id"]),
                "tool": str(use.get("name") or ""),
                "command": use.get("input"),
                "output": _result_text(block.get("content")),
                "is_error": bool(block.get("is_error")),
            }
        )
    return pairs


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
    trees: TreeStore, run_id: str, workspace: Path, session_dir: Path, exclude: Iterable[str] = ()
) -> str:
    """Store the workspace and the session folder as one tree.

    Args:
        trees: The worker's tree store.
        run_id: The job that owns the objects.
        workspace: The agent's workspace.
        session_dir: The bridge's session folder, ``<home>/SESSIONS_DIR/<session id>``.
        exclude: Workspace-relative paths to leave out: the launch files,
            which hold the route key (``harness_bridge.workspace_files``).

    Returns:
        The root tree id.

    Raises:
        ValueError: When the workspace holds an entry named ``SESSIONS_DIR``.
    """
    entries = _put_directory(trees, run_id, workspace, workspace, set(exclude))
    session = trees.put_tree(run_id, _put_directory(trees, run_id, session_dir, session_dir, set()))
    sessions = trees.put_tree(run_id, [TreeEntry(session_dir.name, "tree", session)])
    return trees.put_tree(run_id, [*entries, TreeEntry(SESSIONS_DIR, "tree", sessions)])


def _put_directory(trees: TreeStore, run_id: str, root: Path, directory: Path, exclude: set[str]) -> list[TreeEntry]:
    """Store every file, link and folder below ``directory`` and return its entries.

    Args:
        trees: The tree store.
        run_id: The job that owns the objects.
        root: The folder that ``exclude`` paths are relative to.
        directory: The folder to store.
        exclude: Root-relative POSIX paths to leave out.

    Returns:
        The folder's tree entries. Sockets, pipes and devices are left out.
    """
    entries = []
    for path in sorted(directory.iterdir()):
        if path.relative_to(root).as_posix() in exclude:
            continue
        # A background job can delete a file during the walk; the tree then holds what is still there.
        try:
            info = path.lstat()
            if stat.S_ISLNK(info.st_mode):
                entries.append(TreeEntry(path.name, "symlink", trees.put_blob(run_id, os.fsencode(path.readlink()))))
            elif stat.S_ISDIR(info.st_mode):
                subtree = trees.put_tree(run_id, _put_directory(trees, run_id, root, path, exclude))
                entries.append(TreeEntry(path.name, "tree", subtree))
            elif stat.S_ISREG(info.st_mode):
                mode = "exec" if info.st_mode & 0o111 else "file"
                entries.append(TreeEntry(path.name, mode, trees.put_blob(run_id, path.read_bytes())))
        except FileNotFoundError:
            continue
    return entries


def restore_session(trees: TreeStore, run_id: str, tree_id: str, workspace: Path, home: Path) -> None:
    """Write a :func:`snapshot_session` tree back: the workspace, and the session folder under ``home``.

    Args:
        trees: The tree store.
        run_id: The job that reads the objects.
        tree_id: The root tree id of a committed step.
        workspace: The agent's workspace in the new box.
        home: The home folder in the new box; the bridge reads ``home/SESSIONS_DIR``.

    Raises:
        ValueError: When an object is missing or an entry name is not a plain name.
    """
    for entry in _entries(trees, run_id, tree_id):
        target = home / SESSIONS_DIR if entry.name == SESSIONS_DIR else workspace / entry.name
        _restore(trees, run_id, entry, target)


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
        for child in _entries(trees, run_id, entry.object_id):
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
