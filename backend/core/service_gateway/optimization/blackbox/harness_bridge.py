"""Drive any Skynet agent harness as the proposer of an upstream engine.

The native engines expect a ``claude`` command line (Meta-Harness,
AutoResearch) or a provider object (AutoSaddler). This module ships into the
sandbox next to the runners, imports only the standard library, and turns one
proposer session into a harness launch: it writes the harness configuration
files and the prompt, runs the harness command, parses its transcript for the
final answer and token counts, and reports the outcome in the shape the
upstream code reads. Invoked as the ``claude`` executable it answers the
CLI's ``--print`` calling convention; imported, it exposes the same session
runner to the AutoSaddler provider.
"""

from __future__ import annotations

import argparse
import contextlib
import ctypes
import fcntl
import hashlib
import json
import os
import posixpath
import re
import select
import shlex
import shutil
import signal
import subprocess
import sys
import tempfile
import threading
import time
import uuid
from collections.abc import Iterable, Iterator, Mapping
from dataclasses import dataclass, field
from pathlib import Path, PurePosixPath
from typing import IO, Any

Usage = dict[str, int]

CONFIG_ENV = "APP_PROPOSER_CONFIG"
KEY_ENV = "ASCENT_MODEL_KEY"
# The serialized launch travels through the sandbox input file, so the gateway
# key is replaced by this token and only restored from the process environment.
# The openascent parser refuses it in the contract file's own commands (SKY-776).
KEY_TOKEN = "__ASCENT_MODEL_KEY__"
# The key tokens before SKY-776. The bridge no longer fills them, so a saved launch that still holds one is refused:
# otherwise its harness would send the token's text as the key and fail auth with no clear reason.
_OLD_KEY_TOKEN = re.compile(r"__(?:APP|SKYNET)_API_KEY__")
# The open standard's name for the task file; a custom command reads it (SKY-753).
TASK_ENV = "ASCENT_TASK"
# The same allowance as repo_workspace's setup command; this file runs alone
# in the sandbox, so it cannot import that constant.
SETUP_TIMEOUT_SECONDS = 1_800.0
# The folder in the git dir that keeps ``[proposer] setup``'s record and a
# copy of its output. ``git clean -fdx`` never touches the git dir, and every
# worktree of a checkout shares its common dir, so the setup runs once per git
# checkout and its worktrees. Each Auto lane checkout runs it again (SKY-753).
SETUP_STATE = "ascent-proposer-setup"
# repo_tree.SAFE_GIT, which this lone file cannot import: the setup undo's git
# calls refuse an implicit bare repository layout, as Skynet's other git calls do (SKY-812).
_SAFE_GIT = ("-c", "safe.bareRepository=explicit")
# An exclude line is a pattern, so these characters in a setup's file name are escaped.
_GLOB_CHARS = re.compile(r"([*?[\\])")
# An exclude line cannot hold these: a newline starts a new pattern, and git
# drops trailing whitespace, so such a name could hide other files. A name that
# is not UTF-8 decodes to lone surrogates, which the UTF-8 exclude file cannot hold.
_CONTROL_CHARS = re.compile(r"[\x00-\x1f\x7f\udc80-\udcff]")
# The setup and harness processes this bridge runs now, by pid, with their
# start times. A stop signal to the bridge kills their groups (SKY-753).
_OWNED_GROUPS: dict[int, tuple[subprocess.Popen[Any], int | None]] = {}
# A stop signal that lands while a group starts waits until the group is in
# ``_OWNED_GROUPS``, so the stop also kills it. Masking the signal instead
# would leave it masked in the child (SKY-753).
_STARTING = [0]
_DEFERRED_STOP: list[int] = []
# This file runs alone in the sandbox, without ``core.config``; saved custom
# harnesses and older callers still use these pre-rename spellings.
_LEGACY_PREFIX = "SKYNET_"
SHIM_DIR = ".local/app-bin"
SESSIONS_DIR = ".app-bridge"
# Inside a session folder: the exec shim wrappers and the log they append to (``exec_shim``).
EXEC_SHIM_DIR = "exec-bin"
EXEC_LOG = "exec-log.jsonl"
_SLUG_RE = re.compile(r"[^A-Za-z0-9-]")
_USAGE_KEYS = ("input_tokens", "output_tokens")
# The instruction file every harness reads on start, pointing it at the skills
# the upstream engines drop under ``.claude``.
_POINTER_NOTE = (
    "# Workspace notes\n\n"
    "Project instructions and skills for this task, if any, live under `.claude/` in this directory: read "
    "`CLAUDE.md` and every `.claude/skills/*/SKILL.md` before starting, and follow them as if they were "
    "written for you.\n"
)
_RESUME_NOTE = (
    "\n\n---\n\nA previous session already worked on this task in this directory. Its files and progress are "
    "still here; continue from the current workspace state.\n\n"
)


def _env(name: str, default: str = "") -> str:
    """Read a variable; an ``APP_*`` name falls back to its pre-rename ``SKYNET_*`` name.

    The ``ASCENT_*`` names of the open standard have no legacy name (SKY-776).

    Args:
        name: Variable name.
        default: Value returned when no name is set.

    Returns:
        The first non-empty value found, else ``default``.
    """
    legacy = os.environ.get(_LEGACY_PREFIX + name.removeprefix("APP_")) if name.startswith("APP_") else None
    return os.environ.get(name) or legacy or default


def _with_key(text: str, api_key: str) -> str:
    """Restore the gateway key in place of the key token.

    Args:
        text: A launch command, file or environment value.
        api_key: The route key.

    Returns:
        ``text`` with every key token replaced.
    """
    return text.replace(KEY_TOKEN, api_key)


def _refuse_old_key_token(proposer: dict[str, Any]) -> None:
    """Refuse a launch whose command, environment or files still hold a key token from before SKY-776.

    The contract file's own command (``file_command``) is the user's text, so it is not checked here.

    Args:
        proposer: The serialized launch.

    Raises:
        RuntimeError: When a part of the launch holds an old key token.
    """
    parts = {
        **({} if proposer.get("file_command") else {"run_command": proposer["run_command"]}),
        **{f"env {name}": value for name, value in (proposer.get("env") or {}).items()},
        **{f"file {path}": content for path, content in (proposer.get("files") or {}).items()},
    }
    for where, text in parts.items():
        old = _OLD_KEY_TOKEN.search(text)
        if old:
            raise RuntimeError(
                f"The proposer launch's {where} holds the old key token {old.group()}. Use {KEY_TOKEN} in its place,"
                " or start the run again."
            )


def _json_lines(stdout: str) -> Iterator[dict[str, Any]]:
    """Yield every line of ``stdout`` that parses as a JSON object.

    Args:
        stdout: Captured harness output.

    Yields:
        Parsed event objects, skipping anything that is not JSON.
    """
    for line in stdout.splitlines():
        line = line.strip()
        if not line.startswith("{"):
            continue
        try:
            event = json.loads(line)
        except ValueError:
            continue
        if isinstance(event, dict):
            yield event


def _usage(input_tokens: Any, output_tokens: Any) -> Usage:
    """Normalize a token pair into the usage shape the run record carries.

    Args:
        input_tokens: Prompt tokens, or ``None``.
        output_tokens: Completion tokens, or ``None``.

    Returns:
        ``{"input_tokens": n, "output_tokens": m}`` with missing values as 0.
    """
    return {"input_tokens": int(input_tokens or 0), "output_tokens": int(output_tokens or 0)}


def _add_usage(total: Usage, extra: Usage) -> Usage:
    """Sum two usage records.

    Args:
        total: Running total.
        extra: Usage to add.

    Returns:
        The summed usage.
    """
    return {key: total.get(key, 0) + extra.get(key, 0) for key in _USAGE_KEYS}


def parse_plain_output(stdout: str) -> tuple[str | None, Usage]:
    """Treat the whole output as the answer; nothing reports usage.

    Args:
        stdout: Captured harness output.

    Returns:
        The stripped output (``None`` when empty) and an empty usage record.
    """
    text = stdout.strip()
    return (text or None), {}


def parse_pi_output(stdout: str) -> tuple[str | None, Usage]:
    """Read the last assistant message and summed usage from Pi's ``--mode json`` stream.

    Args:
        stdout: Captured harness output.

    Returns:
        The final assistant text (``None`` when absent) and the usage total.
    """
    text: str | None = None
    usage: Usage = {}
    for event in _json_lines(stdout):
        if event.get("type") != "message_end":
            continue
        message = event.get("message") or {}
        if message.get("role") != "assistant":
            continue
        parts = [part.get("text", "") for part in message.get("content") or [] if part.get("type") == "text"]
        if any(parts):
            text = "\n".join(part for part in parts if part).strip()
        used = message.get("usage") or {}
        usage = _add_usage(usage, _usage(used.get("input"), used.get("output")))
    return text, usage


def parse_codex_output(stdout: str) -> tuple[str | None, Usage]:
    """Read the last agent message and turn usage from ``codex exec --json``.

    Args:
        stdout: Captured harness output.

    Returns:
        The final agent text (``None`` when absent) and the usage total.
    """
    text: str | None = None
    usage: Usage = {}
    for event in _json_lines(stdout):
        kind = event.get("type")
        if kind == "item.completed":
            item = event.get("item") or {}
            if item.get("type") == "agent_message" and item.get("text"):
                text = str(item["text"]).strip()
        elif kind == "turn.completed":
            used = event.get("usage") or {}
            usage = _add_usage(usage, _usage(used.get("input_tokens"), used.get("output_tokens")))
    return text, usage


def parse_opencode_output(stdout: str) -> tuple[str | None, Usage]:
    """Read the last completed text part and summed step usage from ``opencode run --format json``.

    Args:
        stdout: Captured harness output.

    Returns:
        The final assistant text (``None`` when absent) and the usage total.
    """
    text: str | None = None
    usage: Usage = {}
    for event in _json_lines(stdout):
        kind = event.get("type")
        part = event.get("part") or {}
        if kind == "text" and part.get("text"):
            text = str(part["text"]).strip()
        elif kind == "step_finish":
            tokens = part.get("tokens") or {}
            usage = _add_usage(usage, _usage(tokens.get("input"), tokens.get("output")))
    return text, usage


def parse_host_agent_output(stdout: str) -> tuple[str | None, Usage]:
    """Read the summary line the host-agent shim prints after a host CLI turn.

    Args:
        stdout: Captured shim output.

    Returns:
        The CLI's final text (``None`` when absent) and its reported usage.
    """
    text: str | None = None
    usage: Usage = {}
    for event in _json_lines(stdout):
        if event.get("type") != "host_agent.result":
            continue
        text = str(event.get("text") or "").strip() or None
        used = event.get("usage") or {}
        usage = _add_usage(usage, _usage(used.get("input_tokens"), used.get("output_tokens")))
    return text, usage


def parse_host_agent_refusals(stdout: str) -> list[dict[str, str]]:
    """Read the hidden-path refusals the host-agent shim reports for a host CLI turn.

    The parent runs the host CLI, so its refusals reach the session only
    through the shim's summary line, not through the session's guard log.

    Args:
        stdout: Captured shim output.

    Returns:
        One ``{"tool", "kind"}`` entry per refusal.
    """
    refusals: list[dict[str, str]] = []
    for event in _json_lines(stdout):
        if event.get("type") != "host_agent.result" or not isinstance(event.get("refusals"), list):
            continue
        refusals.extend(
            {"tool": entry["tool"], "kind": entry["kind"]}
            for entry in event["refusals"]
            if isinstance(entry, dict) and isinstance(entry.get("tool"), str) and isinstance(entry.get("kind"), str)
        )
    return refusals


# Codex items that are the agent acting rather than talking or thinking; each
# is announced on ``item.started`` and settled on ``item.completed``.
_CODEX_TOOL_ITEMS = frozenset({"command_execution", "file_change", "mcp_tool_call", "web_search"})


def tool_call_ids(output_format: str, event: dict[str, Any]) -> list[str]:
    """Name the tool calls one streamed harness event starts.

    Args:
        output_format: One of the ``PARSERS`` keys.
        event: One parsed JSON event from the harness stdout.

    Returns:
        One identifier per tool call the event announces; an empty identifier
        stands for a call the event does not name. Formats that stream no tool
        events (``plain``) never yield any.
    """
    kind = event.get("type")
    if output_format == "pi":
        return [str(event.get("toolCallId") or "")] if kind == "tool_execution_start" else []
    if output_format == "codex":
        item = event.get("item") or {}
        if kind in ("item.started", "item.completed") and item.get("type") in _CODEX_TOOL_ITEMS:
            # A file change may only ever be reported as completed, so both
            # events count and the item id keeps a started call from counting twice.
            return [str(item.get("id") or "")]
        return []
    if output_format == "opencode":
        part = event.get("part") or {}
        return [str(part.get("callID") or part.get("id") or "")] if kind == "tool_use" else []
    return []


PARSERS = {
    "plain": parse_plain_output,
    "pi": parse_pi_output,
    "codex": parse_codex_output,
    "opencode": parse_opencode_output,
    "host_agent": parse_host_agent_output,
}


def parse_output(output_format: str, stdout: str) -> tuple[str | None, Usage]:
    """Parse a harness transcript in the named format.

    Args:
        output_format: One of the ``PARSERS`` keys.
        stdout: Captured harness output.

    Returns:
        The final answer text (``None`` when absent) and the usage total.
    """
    return PARSERS[output_format](stdout)


def contained_path(root: Path, relative_path: str) -> Path:
    """Resolve a workspace-relative file path, refusing anything that escapes.

    Args:
        root: Workspace directory.
        relative_path: POSIX path relative to the workspace.

    Returns:
        The absolute path inside the workspace.

    Raises:
        ValueError: When the path is absolute or climbs out of the workspace.
    """
    path = PurePosixPath(relative_path)
    if path.is_absolute() or ".." in path.parts:
        raise ValueError(f"Harness file path must be workspace-relative: {relative_path}")
    target = root.joinpath(*path.parts)
    if not target.resolve().is_relative_to(root.resolve()):
        raise ValueError(f"Harness file escapes the workspace: {relative_path}")
    return target


def workspace_files(proposer: dict[str, Any]) -> list[str]:
    """List the files :func:`run_session` writes into the agent's workspace.

    Args:
        proposer: Serialized harness launch.

    Returns:
        Workspace-relative paths; empty without a proposer.
    """
    if not proposer:
        return []
    paths = [str(path) for path in (proposer.get("files") or {})]
    instructions = proposer.get("instructions_file")
    if instructions and instructions != "CLAUDE.md":
        paths.append(str(instructions))
    return paths


def run_setup(
    command: list[str],
    checkout: Path,
    timeout_seconds: float = SETUP_TIMEOUT_SECONDS,
    env: Mapping[str, str] | None = None,
) -> None:
    """Run the contract file's ``[proposer] setup`` in the folder its command runs in.

    Args:
        command: The exact argument list from ``ascent.toml``.
        checkout: The repository checkout or worktree of the next session.
        timeout_seconds: Stop the setup, and every process it started, after this long.
        env: The setup's environment; the default is this process's environment.

    Raises:
        RuntimeError: When the command cannot start, runs out of time or
            exits with an error, so no session starts without its setup.
    """
    label = f"The [proposer] setup command in ascent.toml ({shlex.join(command)})"
    reaped = sys.platform == "linux" and bool(sys.executable)
    if reaped:
        # The same subreaper as a session, so a setsid or double-forked child cannot escape the group kill.
        command = [sys.executable, "-I", str(Path(__file__).resolve()), "reaper", *command]
    # Files, not pipes: a background child keeps an inherited pipe open, and a
    # read to its end would wait for that child instead of for the setup.
    with tempfile.TemporaryFile() as stdout_file, tempfile.TemporaryFile() as stderr_file:
        with _defer_stop():
            try:
                process = subprocess.Popen(
                    command,
                    cwd=checkout,
                    env=env,
                    stdin=subprocess.DEVNULL,
                    stdout=stdout_file,
                    stderr=stderr_file,
                    start_new_session=True,
                )
            except OSError as error:
                raise RuntimeError(f"{label} could not start: {error}") from error
            root_start = _start_time(process.pid)
            _OWNED_GROUPS[process.pid] = (process, root_start)
        try:
            finished = _wait_unreaped(process.pid, timeout_seconds)
            # A process the setup left in the background (a watcher, a server)
            # must not run beside the proposer sessions, so the group stops
            # after a finished setup too. The setup is not reaped yet, so its
            # pid, and the group id, cannot belong to another process.
            _stop_group(process, signal.SIGKILL, root_start)
            process.wait()
        finally:
            _OWNED_GROUPS.pop(process.pid, None)
        if not finished:
            raise RuntimeError(f"{label} did not finish within {timeout_seconds:g} seconds.")
        if process.returncode != 0:
            detail = (_tail(stderr_file) or _tail(stdout_file)).strip()[-2000:]
            if reaped and process.returncode in (_REAPER_NOT_FOUND, _REAPER_NOT_EXECUTABLE):
                raise RuntimeError(f"{label} could not start: {detail}")
            raise RuntimeError(f"{label} failed with exit code {process.returncode}: {detail}")


def _wait_unreaped(pid: int, timeout_seconds: float) -> bool:
    """Wait for a child to end, and leave it unreaped so its pid stays its own.

    Args:
        pid: The child's process id.
        timeout_seconds: How long to wait.

    Returns:
        True when the child ended, False when the time ran out.
    """
    if not hasattr(os, "waitid"):
        # macOS has no waitid. Its kqueue reports the exit without reaping the
        # child, and reports an already ended child at once, with an error flag.
        queue = select.kqueue()
        try:
            event = select.kevent(
                pid, select.KQ_FILTER_PROC, select.KQ_EV_ADD | select.KQ_EV_ONESHOT, select.KQ_NOTE_EXIT
            )
            return bool(queue.control([event], 1, timeout_seconds))
        finally:
            queue.close()
    deadline = time.monotonic() + timeout_seconds
    while os.waitid(os.P_PID, pid, os.WEXITED | os.WNOHANG | os.WNOWAIT) is None:
        if time.monotonic() >= deadline:
            return False
        time.sleep(0.05)
    return True


def _stop_owned_groups(signum: int, _frame: Any) -> None:
    """Kill the setup and session groups this bridge started, then exit.

    The upstream drivers stop a session with SIGTERM to the bridge alone.
    The setup and the harness run in their own sessions, so without this
    they would outlive a cancelled run, and the next session would start a
    second setup in the same checkout (SKY-753). The exit skips the undo of
    a setup that has not finished; the next session does that undo from the
    copy that :func:`ensure_setup` keeps.

    Args:
        signum: The signal that stops the bridge.
        _frame: The interrupted frame.
    """
    if _STARTING[0]:
        _DEFERRED_STOP.append(signum)
        return
    for process, root_start in list(_OWNED_GROUPS.values()):
        # Without /proc (macOS) nothing proves a reaped pid is still ours.
        if process.returncode is None or root_start is not None:
            _stop_group(process, signal.SIGKILL, root_start)
    os._exit(128 + signum)


@contextlib.contextmanager
def _defer_stop() -> Iterator[None]:
    """Hold a stop signal while a group starts, then handle it once the group is owned.

    Yields:
        Nothing; the block starts one process group and adds it to ``_OWNED_GROUPS``.
    """
    _STARTING[0] += 1
    try:
        yield
    finally:
        _STARTING[0] -= 1
        if _DEFERRED_STOP and not _STARTING[0]:
            _stop_owned_groups(_DEFERRED_STOP[0], None)


def _tail(file: IO[bytes]) -> str:
    """Read the end of a setup's output file.

    A package install can write megabytes, and the error keeps only the end.

    Args:
        file: The output file, at any position.

    Returns:
        The last 8 KiB of the file as text.
    """
    file.seek(max(0, file.seek(0, os.SEEK_END) - 8192))
    return file.read().decode(errors="replace")


def _checkout_git(arguments: list[str], checkout: Path, stdin: bytes = b"") -> str:
    """Run one git command in a checkout and return its output.

    Args:
        arguments: Arguments after ``git``.
        checkout: The checkout or worktree.
        stdin: The command's standard input.

    Returns:
        The command's standard output.

    Raises:
        RuntimeError: When git fails.
    """
    done = subprocess.run(["git", *_SAFE_GIT, *arguments], cwd=checkout, input=stdin, capture_output=True, check=False)
    # Bytes, not text mode: text mode turns a ``\r`` in a file name into ``\n``.
    stdout, stderr = (stream.decode("utf-8", "surrogateescape") for stream in (done.stdout, done.stderr))
    if done.returncode != 0:
        raise RuntimeError(f"git {arguments[0]} failed: {(stderr or stdout).strip()[-2000:]}")
    return stdout


def _untracked(checkout: Path, *, folders: bool, ignored: bool = False, empty: bool = False) -> set[str]:
    """List the untracked paths of a checkout, either those git ignores or those it does not.

    Args:
        checkout: The checkout or worktree.
        folders: List a folder that holds only untracked files as one path
            that ends with ``/``, in place of each file in it.
        ignored: List the ignored paths in place of the not ignored ones.
        empty: With ``folders``, list the empty folders too.

    Returns:
        Checkout-relative paths.
    """
    arguments = [
        "ls-files",
        "-z",
        "--others",
        *(["--ignored"] if ignored else []),
        "--exclude-standard",
        # An empty folder holds no file of the version or of the setup, so it is no output either.
        *(["--directory"] if folders else []),
        *(["--no-empty-directory"] if folders and not empty else []),
    ]
    return {path for path in _checkout_git(arguments, checkout).split("\0") if path}


def _setup_output(checkout: Path, before: set[str], folders: set[str], *, ignored: bool = False) -> set[str]:
    """Find the untracked paths that a setup added to a checkout.

    Args:
        checkout: The checkout or worktree, after the setup.
        before: Every untracked file of the same kind before the setup, one path per file.
        folders: :func:`_old_folders` from before the setup.
        ignored: Find the paths that git ignores in place of the not ignored ones.

    Returns:
        The new files, and the new folders as one path each, so a large
        install folder gives one exclude line.
    """
    added: set[str] = set()
    files: set[str] | None = None
    for path in _untracked(checkout, folders=True, ignored=ignored):
        if not path.endswith("/"):
            if path not in before:
                added.add(path)
        elif path in folders or any(old.startswith(path) for old in before):
            # The folder was there before the setup, empty or with files of the
            # version, so it stays: only the files the setup wrote into it are excluded.
            files = _untracked(checkout, folders=False, ignored=ignored) if files is None else files
            added |= {file for file in files if file.startswith(path)} - before
        else:
            added.add(path)
    return added


def _old_folders(checkout: Path) -> set[str]:
    """List the untracked folders of a checkout, ignored and empty ones included, before a setup.

    Args:
        checkout: The checkout or worktree.

    Returns:
        Checkout-relative folder paths that end with ``/``; a folder with only untracked content is one path.
    """
    listed = _untracked(checkout, folders=True, empty=True) | _untracked(
        checkout, folders=True, ignored=True, empty=True
    )
    return {path for path in listed if path.endswith("/")}


def _index_stamp(checkout: Path) -> list[int] | None:
    """Identify the current git index file of a checkout.

    Git writes a new index file with a rename, so any git command that changes the index, or
    ``git reset --hard`` even with no change, gives it a new stamp. Reading commands keep it.

    Args:
        checkout: The checkout or worktree.

    Returns:
        The index file's inode, modification time in nanoseconds and size, or ``None`` when it has no index.
    """
    try:
        status = _index_path(checkout).stat()
    except FileNotFoundError:
        return None
    return [status.st_ino, status.st_mtime_ns, status.st_size]


def _tracked_state(checkout: Path, path: str) -> str:
    """Describe the current content of one path of the version.

    Args:
        checkout: The checkout or worktree.
        path: A checkout-relative path that git tracks, or an untracked file of the version.

    Returns:
        A hash of the file, or ``"missing"`` when the path is gone.
    """
    file = checkout / path
    if file.is_symlink():
        return f"link:{file.readlink()}"
    return hashlib.sha256(file.read_bytes()).hexdigest() if file.is_file() else "missing"


def _tracked_changes(checkout: Path) -> dict[str, str]:
    """List the tracked paths that differ from the checkout's commit, with their content.

    Args:
        checkout: The checkout or worktree.

    Returns:
        Each changed path and its :func:`_tracked_state`.
    """
    paths = _checkout_git(["diff", "--name-only", "--no-renames", "-z", "HEAD"], checkout).split("\0")
    return {path: _tracked_state(checkout, path) for path in paths if path}


def _setup_changes(checkout: Path, tracked: dict[str, str]) -> list[str]:
    """List the tracked paths whose content differs from their content before the setup.

    Args:
        checkout: The checkout or worktree, after the setup.
        tracked: :func:`_tracked_changes` before the setup.

    Returns:
        The changed paths, sorted.
    """
    return sorted(
        path
        for path in tracked.keys() | _tracked_changes(checkout).keys()
        if path not in tracked or tracked[path] != _tracked_state(checkout, path)
    )


def _index_path(checkout: Path) -> Path:
    """Find the git index file of a checkout or worktree.

    Args:
        checkout: The checkout or worktree.

    Returns:
        The absolute path of its index file.
    """
    index = Path(_checkout_git(["rev-parse", "--git-path", "index"], checkout).strip())
    return index if index.is_absolute() else checkout / index


def _index_lock(checkout: Path) -> Path:
    """Find the lock file that git makes beside the index while a command writes it.

    Args:
        checkout: The checkout or worktree.

    Returns:
        The absolute path of the lock file.
    """
    index = _index_path(checkout)
    return index.with_name(index.name + ".lock")


def _common_dir(checkout: Path) -> Path:
    """Find the git common dir that a checkout and its worktrees share.

    Args:
        checkout: The checkout or worktree.

    Returns:
        The absolute path of the common dir.
    """
    common = Path(_checkout_git(["rev-parse", "--git-common-dir"], checkout).strip())
    return (common if common.is_absolute() else checkout / common).resolve()


def _branch_refs(checkout: Path) -> dict[str, str]:
    """List every local branch of a checkout's repository with its commit.

    Args:
        checkout: The checkout or worktree.

    Returns:
        Each full branch ref name and its commit.
    """
    lines = _checkout_git(["for-each-ref", "--format=%(refname) %(objectname)", "refs/heads"], checkout).splitlines()
    # A ref name cannot hold a space, so the first space ends it.
    return dict(line.split(" ", 1) for line in lines if line)


def _save_checkout(checkout: Path, common: Path, paths: Iterable[str], saved: Path) -> None:
    """Copy the index, ``info/exclude`` and the version's changed files before a setup, so the setup can be undone.

    Args:
        checkout: The checkout or worktree.
        common: The git common dir.
        paths: The tracked paths the version changed and its untracked files.
        saved: An empty folder for the copy.
    """
    index = _index_path(checkout)
    if index.exists():
        shutil.copy2(index, saved / "index")
    exclude = common / "info" / "exclude"
    if exclude.exists():
        (saved / "exclude").write_bytes(exclude.read_bytes())
    for path in paths:
        if os.path.lexists(checkout / path):
            _copy_path(checkout / path, saved / "files" / path, saved / "files")


def _undo_setup(checkout: Path, common: Path, saved: Path, started: dict[str, Any], *, later: bool = False) -> None:
    """Put a repository and the checkout of a setup back to their state before a setup that did not finish.

    Args:
        checkout: The checkout of the session that runs the undo.
        common: The git common dir.
        saved: The :func:`_save_checkout` copy from before the setup.
        started: The record that :func:`ensure_setup` wrote before the setup: the setup's checkout, ``head``,
            ``branch`` (``None`` when ``HEAD`` was detached), ``refs`` from :func:`_branch_refs`, ``tracked``
            from :func:`_tracked_changes`, ``version_files`` with each untracked file of the version and its
            :func:`_tracked_state`, ``ignored_before`` with every ignored file, ``folders_before`` from
            :func:`_old_folders`, ``index_stamp`` from :func:`_index_stamp` and ``index_lock``, whether
            the index lock file was there.
        later: The undo runs in a later session, so the engine can have moved the checkout to another version.

    Raises:
        RuntimeError: When git fails, or an index lock file that was there before the setup is still there.
    """
    target = Path(started["checkout"])
    try:
        # A killed session can have run in another worktree of the repository, and that worktree can be gone now.
        present = _common_dir(target) == common
    except (RuntimeError, OSError):
        present = False
    # Read before the refs go back, because the restored branch would hide a move of HEAD.
    moved = (
        later
        and present
        and (
            _checkout_git(["rev-parse", "HEAD"], target).strip() != started["head"]
            or _index_stamp(target) != started["index_stamp"]
        )
    )
    git_folder = target if present else checkout
    refs = started["refs"]
    current = _branch_refs(git_folder)
    for ref in current.keys() - refs.keys():
        _checkout_git(["update-ref", "-d", ref], git_folder)
    for ref, commit in refs.items():
        if current.get(ref) != commit:
            _checkout_git(["update-ref", ref, commit], git_folder)
    exclude = common / "info" / "exclude"
    if (saved / "exclude").exists():
        exclude.parent.mkdir(parents=True, exist_ok=True)
        exclude.write_bytes((saved / "exclude").read_bytes())
    elif os.path.lexists(exclude):
        exclude.unlink()
    if not present:
        return
    checkout, head, tracked = target, started["head"], started["tracked"]
    version_files: dict[str, str] = started["version_files"]
    if started["branch"]:
        # The branch refs are back, so this also puts back the commit of HEAD.
        _checkout_git(["symbolic-ref", "HEAD", started["branch"]], checkout)
    else:
        _checkout_git(["update-ref", "--no-deref", "HEAD", head], checkout)
    if moved:
        # The engine's reset_tree rewrites the index before it applies the next version; an undo now would put
        # the old version over the new one. A setup's own git write looks the same, and its edits then stay.
        print(
            "claude bridge: the checkout holds another version than before the stopped setup, so its files and"
            " index stay as they are",
            file=sys.stderr,
        )
        return
    lock = _index_lock(checkout)
    if os.path.lexists(lock):
        if started["index_lock"]:
            raise RuntimeError(
                f"The git index lock {lock} was there before the setup and is still there, so the checkout cannot"
                " be restored. Remove that file when no git command runs in the checkout."
            )
        # The setup's process group is stopped, so a lock it left belongs to no running git command.
        print(f"claude bridge: removed the git index lock {lock} that the stopped setup left", file=sys.stderr)
        lock.unlink()
    if (saved / "index").exists():
        shutil.copy2(saved / "index", _index_path(checkout))
    # Listed after the index is back, so a file the setup untracked in its commit counts as tracked again.
    # Removed first, so a link the setup put in place of a folder is gone before the files below are restored.
    # A path that was there before is never output: a setup that rewrites a .gitignore can unhide an old ignored file.
    before = set(version_files) | set(started["ignored_before"])
    folders = set(started["folders_before"])
    for path in _setup_output(checkout, before, folders) | _setup_output(checkout, before, folders, ignored=True):
        _remove_path(checkout / path.rstrip("/"))
    changed = _setup_changes(checkout, tracked)
    # Names go through a whole-tree listing and stdin, because thousands of them would pass the argument limit.
    in_head = (
        set(_checkout_git(["ls-tree", "-r", "-z", "--name-only", head], checkout).split("\0")) if changed else set()
    )
    from_head: list[str] = []
    for path in changed:
        _check_parents(checkout / path, checkout)
        if path in tracked:
            # The version had changed this file before the setup, so the commit's content is not its content.
            _remove_path(checkout / path)
            if os.path.lexists(saved / "files" / path):
                _copy_path(saved / "files" / path, checkout / path, checkout)
        elif path in in_head:
            from_head.append(path)
        else:
            _remove_path(checkout / path)
    if from_head:
        specs = "".join(f":(literal){path}\0" for path in from_head).encode("utf-8", "surrogateescape")
        _checkout_git(["checkout", head, "--pathspec-from-file=-", "--pathspec-file-nul"], checkout, specs)
    for path, content in version_files.items():
        if _tracked_state(checkout, path) != content:
            _check_parents(checkout / path, checkout)
            _remove_path(checkout / path)
            _copy_path(saved / "files" / path, checkout / path, checkout)


def _check_parents(target: Path, root: Path) -> None:
    """Refuse a path whose parent folders do not stay plain folders inside a root.

    Args:
        target: The path to write or remove.
        root: The folder that must hold ``target``.

    Raises:
        RuntimeError: When a parent folder of ``target`` is a link, is not a folder or resolves outside ``root``.
    """
    # A version can replace a parent folder with a link; the copy would then write outside the checkout.
    folder = root
    for part in target.relative_to(root).parts[:-1]:
        folder = folder / part
        if os.path.lexists(folder) and (
            folder.is_symlink() or not folder.is_dir() or not folder.resolve().is_relative_to(root.resolve())
        ):
            raise RuntimeError(
                f"The [proposer] setup output {target} cannot be restored: its parent folder {folder} is a link,"
                f" is not a folder or is outside {root}. Make the version keep that folder a plain folder."
            )


def _copy_path(source: Path, target: Path, root: Path) -> None:
    """Copy one file, link or folder of a setup's output, with its links kept as links.

    Args:
        source: The path to copy.
        target: The new path; its parent folders are made.
        root: The folder that must hold ``target``.

    Raises:
        RuntimeError: When a parent folder of ``target`` is a link, is not a folder or resolves outside ``root``.
    """
    _check_parents(target, root)
    target.parent.mkdir(parents=True, exist_ok=True)
    if source.is_dir() and not source.is_symlink():
        shutil.copytree(source, target, symlinks=True)
    else:
        shutil.copy2(source, target, follow_symlinks=False)


def _remove_path(path: Path) -> None:
    """Remove one file, link or folder, and never follow a link.

    Args:
        path: The path to remove; a missing path is skipped.
    """
    if path.is_dir() and not path.is_symlink():
        shutil.rmtree(path)
    elif os.path.lexists(path):
        path.unlink()


def _unsafe(path: str) -> bool:
    """Tell whether no exclude line can hold a path.

    Args:
        path: A checkout-relative path; a folder ends with ``/``.

    Returns:
        True when the name is not UTF-8, holds a control character or ends in whitespace.
    """
    return bool(_CONTROL_CHARS.search(path)) or path.rstrip("/") != path.rstrip("/").rstrip()


def _excludable(paths: Iterable[str], command: list[str]) -> list[str]:
    """Turn a setup's output paths into exclude lines, and refuse a name no line can hold.

    Args:
        paths: Checkout-relative paths; a folder ends with ``/``.
        command: The setup's argument list, for the error.

    Returns:
        One anchored exclude line per path.

    Raises:
        RuntimeError: When a name is not UTF-8, holds a control character or ends in whitespace.
    """
    paths = list(paths)
    unsafe = sorted(path for path in paths if _unsafe(path))
    if unsafe:
        raise RuntimeError(
            f"The [proposer] setup command in ascent.toml ({shlex.join(command)}) wrote files whose names are"
            " not UTF-8, hold a control character or end in whitespace:"
            f" {', '.join(repr(path) for path in unsafe)}. The run cannot keep such a file out of the versions."
            " Rename it in the setup."
        )
    return ["/" + _GLOB_CHARS.sub(r"\\\1", path) for path in paths]


def ensure_setup(
    command: list[str],
    checkout: Path,
    timeout_seconds: float = SETUP_TIMEOUT_SECONDS,
    env: Mapping[str, str] | None = None,
) -> None:
    """Run ``[proposer] setup`` once per git checkout, and give each later session in it a copy of its output.

    The record is in the git common dir, so the checkout and its worktrees
    share it, and a separate checkout (each Auto lane) runs the setup again.
    The setup runs in the first session. Its output, ignored files
    included, is copied into :data:`SETUP_STATE` in the git dir, and a
    session after a reset or in a new worktree gets that copy back, without
    a second setup. What the setup adds, ignored files included, is excluded
    from every version, like the harness files (SKY-753).

    Before the setup runs, a copy of the checkout's state and a ``started``
    record are kept in :data:`SETUP_STATE`. A failure in this session puts
    the repository back as it was: the branch refs, ``HEAD``, the index,
    ``info/exclude``, the files of the version, and no setup output. A
    session that is killed during its setup cannot do that, so the next
    session that finds the ``started`` record and no ``done.json`` first
    does the same undo from the kept copy, and then runs the setup again.
    When ``HEAD`` or the index file changed since that setup started, the
    engine moved the checkout to another version, so that undo puts back
    only the branch refs, ``HEAD`` and ``info/exclude``.

    Args:
        command: The exact argument list from ``ascent.toml``.
        checkout: The repository checkout or worktree the proposer command runs in.
        timeout_seconds: Stop the setup, and every process it started, after this long.
            The wait for another session's setup counts inside it.
        env: The setup's environment, the same as the proposer command's.

    Raises:
        RuntimeError: When the setup or git fails, the setup changes a file of
            the version or moves ``HEAD``, it writes a name that no exclude
            line can hold, another session's setup holds the lock past
            ``timeout_seconds`` or fails with no time left to run it again,
            the setup's record is unreadable, or the undo of a setup that an
            earlier session did not finish fails.
    """
    common = _common_dir(checkout)
    state = common / SETUP_STATE
    state.mkdir(parents=True, exist_ok=True)
    with (state / "lock").open("a") as lock:
        # Parallel AutoSaddler sessions share the git dir; one runs the setup, the others wait for its output.
        # A blocking flock would wait past the session's time limit, so the wait polls up to that limit.
        deadline = time.monotonic() + timeout_seconds
        while True:
            try:
                fcntl.flock(lock, fcntl.LOCK_EX | fcntl.LOCK_NB)
                break
            except BlockingIOError:
                if time.monotonic() >= deadline:
                    raise RuntimeError(
                        f"The [proposer] setup command in ascent.toml ({shlex.join(command)}) did not finish"
                        f" in another session within {timeout_seconds:g} seconds."
                    ) from None
                time.sleep(0.1)
        record = state / "done.json"
        started = state / "started.json"
        saved = state / "undo"
        if record.exists():
            try:
                kept = json.loads(record.read_text(encoding="utf-8"))["output"]
            except (ValueError, KeyError, TypeError) as error:
                raise RuntimeError(
                    f"The record of the [proposer] setup command in ascent.toml ({shlex.join(command)}) at"
                    f" {record} is unreadable: {error!r}. The run cannot tell which files the setup wrote."
                ) from error
            # A session killed after done.json but before its clean-up left these behind.
            _forget_setup(started, saved)
            # The first session writes the whole copy before done.json, and nothing changes it after,
            # so later sessions copy it unlocked and do not wait in line for each other.
            fcntl.flock(lock, fcntl.LOCK_UN)
            for path in kept:
                name = path.rstrip("/")
                # A file that the version holds at the same path wins over the setup's copy.
                if not os.path.lexists(checkout / name):
                    _copy_path(state / "output" / name, checkout / name, checkout)
            return
        if started.exists():
            # A session killed during its setup skipped its undo; nothing may trust the checkout until it is done.
            try:
                _undo_setup(checkout, common, saved, json.loads(started.read_text(encoding="utf-8")), later=True)
            except Exception as error:
                raise RuntimeError(
                    f"The [proposer] setup command in ascent.toml ({shlex.join(command)}) stopped in an earlier"
                    f" session before it finished, and its changes could not be undone: {error!r}."
                ) from error
        # A copy with no started record is from a session killed before its setup ran, so nothing needs it.
        _forget_setup(started, saved)
        # Files the parent version added are untracked too; only the setup's new paths are excluded.
        before = _untracked(checkout, folders=False)
        # ``git clean -fdx`` and a new worktree drop ignored files too, so the setup's ignored output is kept as well.
        ignored_before = _untracked(checkout, folders=False, ignored=True)
        # A folder that was there before, even an empty one, is never setup output.
        folders_before = _old_folders(checkout)
        version_files = {path: _tracked_state(checkout, path) for path in before}
        head = _checkout_git(["rev-parse", "HEAD"], checkout).strip()
        branch = _checkout_git(["rev-parse", "--symbolic-full-name", "HEAD"], checkout).strip()
        tracked = _tracked_changes(checkout)
        saved.mkdir()
        # The version's own edits are only hashed above, so a failed setup could not put them back.
        _save_checkout(checkout, common, tracked.keys() | before, saved)
        # A waiter whose lock holder failed runs the setup itself, but only in the time its own limit has left.
        # The copy above counts too, because a large version takes time to copy.
        remaining = deadline - time.monotonic()
        if remaining <= 0:
            _forget_setup(started, saved)
            raise RuntimeError(
                f"The [proposer] setup command in ascent.toml ({shlex.join(command)}) did not finish in another"
                f" session, and no time was left within {timeout_seconds:g} seconds to run it in this one."
            )
        start = {
            "checkout": str(checkout.resolve()),
            "head": head,
            "branch": branch if branch.startswith("refs/heads/") else None,
            "refs": _branch_refs(checkout),
            "tracked": tracked,
            "version_files": version_files,
            "ignored_before": sorted(ignored_before),
            "folders_before": sorted(folders_before),
            # Taken after the last git command of this session, so only a later write changes it.
            "index_stamp": _index_stamp(checkout),
            "index_lock": os.path.lexists(_index_lock(checkout)),
        }
        # Written last, so a session that finds it knows the copy above is whole.
        _write_whole(started, json.dumps(start))
        try:
            run_setup(command, checkout, remaining, env)
            moved = _checkout_git(["rev-parse", "HEAD"], checkout).strip()
            if moved != head:
                # A commit would hide its changes from the diff below, and enter every version.
                raise RuntimeError(
                    f"The [proposer] setup command in ascent.toml ({shlex.join(command)}) moved HEAD from"
                    f" {head} to {moved}. Those changes would enter every version. Make the setup leave the git"
                    " history alone."
                )
            changed = _setup_changes(checkout, tracked)
            if changed:
                raise RuntimeError(
                    f"The [proposer] setup command in ascent.toml ({shlex.join(command)}) changed tracked files:"
                    f" {', '.join(changed)}. Those changes would enter every version. Make the setup write only"
                    " files that git does not track."
                )
            # GEPA and Best-of-N check out a parent version with its new files untracked;
            # a change to one of them would enter every version as well.
            rewritten = sorted(
                path for path, content in version_files.items() if _tracked_state(checkout, path) != content
            )
            if rewritten:
                raise RuntimeError(
                    f"The [proposer] setup command in ascent.toml ({shlex.join(command)}) changed or removed"
                    f" files of the version: {', '.join(rewritten)}. Those changes would enter every version."
                    " Make the setup write only new files that git does not track."
                )
            added = sorted(_setup_output(checkout, before, folders_before))
            ignored = _setup_output(checkout, ignored_before, folders_before, ignored=True)
            # A version can drop the .gitignore line that hides an ignored output path, so it gets an exclude line
            # too. Git ignores it now, so a name that no line can hold stays hidden by .gitignore alone.
            lines = _excludable(added, command) + _excludable(sorted(p for p in ignored if not _unsafe(p)), command)
            kept = sorted(set(added) | ignored)
            output = state / "output"
            shutil.rmtree(output, ignore_errors=True)
            for path in kept:
                name = path.rstrip("/")
                # An ignored path inside a new folder is already in that folder's copy.
                if not os.path.lexists(output / name):
                    _copy_path(checkout / name, output / name, output)
            exclude = common / "info" / "exclude"
            exclude.parent.mkdir(parents=True, exist_ok=True)
            known = set(exclude.read_text(encoding="utf-8").splitlines()) if exclude.exists() else set()
            with exclude.open("a", encoding="utf-8") as stream:
                stream.write("".join(f"\n{line}" for line in lines if line not in known) + "\n")
            _write_whole(record, json.dumps({"output": kept}))
        except BaseException as error:
            # Any failure after the setup starts leaves the checkout as it was, so no edit becomes the baseline.
            try:
                _undo_setup(checkout, common, saved, start)
                _forget_setup(started, saved)
            except Exception as undo_error:
                # The started record stays, so the next session tries the undo again before its setup.
                error.add_note(f"The checkout could not be restored after the setup: {undo_error!r}")
            raise
        _forget_setup(started, saved)


def _write_whole(path: Path, text: str) -> None:
    """Replace a file whole, so a stop during the write never leaves a partial file.

    Args:
        path: The file to write.
        text: Its new content.
    """
    partial = path.with_name(path.name + ".tmp")
    partial.write_text(text, encoding="utf-8")
    partial.replace(path)


def _forget_setup(started: Path, saved: Path) -> None:
    """Remove a setup's started record, and then the copy of the checkout that the undo needs.

    The record goes first: a stop between the two leaves only a copy, which
    the next session removes.

    Args:
        started: The started record.
        saved: The :func:`_save_checkout` copy.
    """
    started.unlink(missing_ok=True)
    shutil.rmtree(saved, ignore_errors=True)


def cost_usd(usage: Usage, price: dict[str, float] | None) -> float:
    """Price a usage record with the per-token rates the parent supplied.

    Args:
        usage: Token counts.
        price: ``{"input": usd, "output": usd}`` per token, or ``None`` when unknown.

    Returns:
        The estimated spend in USD; zero when no price is known.
    """
    if not price:
        return 0.0
    return usage.get("input_tokens", 0) * float(price.get("input", 0.0)) + usage.get("output_tokens", 0) * float(
        price.get("output", 0.0)
    )


GUARD_DIR = "guard"
GUARD_REFUSAL = (
    "Refused: this path is hidden from the agent. It belongs to the run's scoring side. Continue the task without it."
)
# Prime Agent has one tool, ``ipython``, whose ``code`` is Python, not a shell command.
_GUARD_COMMAND_KEYS = frozenset({"command", "cmd", "script", "code"})
_GUARD_PATH_KEYS = frozenset({"pattern", "glob", "dir", "directory", "include", "target"})
_GUARD_BASE_KEYS = frozenset({"workdir", "cwd", "working_directory"})
_GUARD_CD_WORDS = frozenset({"cd", "pushd"})
_GUARD_PATCH_RE = re.compile(r"^\*\*\* (?:(?:Add|Update|Delete) File|Move to): (.+)$", re.MULTILINE)
_GUARD_WORD_RE = re.compile(r"[\w.~/@+-]+")
_GUARD_PWD_RE = re.compile(r"\$\{PWD\}|\$PWD\b|\$\(\s*pwd\s*\)|`\s*pwd\s*`")
# A tilde names the home folder only at the start of a word, as in the shell.
_GUARD_HOME_RE = re.compile(r"\$\{HOME\}|\$HOME\b|(?<![^\s;|&()<>=:'\"`])~(?=/|$|[\s;|&()<>'\"`])")
# A run of path text that follows one of these characters starts after a shell
# expansion the guard cannot resolve, such as ``${PWD:-x}`` or ``$(pwd -P)``.
_GUARD_DYNAMIC_PREFIX = frozenset("$)}`")
_GUARD_SHELL_DEPTH = 3
# A hook that the harness kills at its timeout lets the call run. These caps
# keep the worst call the guard accepts far below that timeout; a call above a
# cap is refused.
_GUARD_MAX_BASES = 4
_GUARD_MAX_TEXT = 64_000
_GUARD_MAX_CHECKS = 8_000
_GUARD_MAX_PARTS = 32
# The agent can make links in the workspace, so link resolution is part of the
# cost. Each path part the resolver looks at counts, link or not, because a
# link can point at a long path of parts that are not links. Linux also
# refuses a path behind more than 40 links (ELOOP).
_GUARD_MAX_LINK_STEPS = 200_000
_GUARD_MAX_LINK_DEPTH = 40
_GUARD_DEADLINE_SECONDS = 5.0
# Options of these commands that name the folder the command works in.
_GUARD_DIR_OPTIONS = frozenset({"-C", "--chdir", "--directory", "--cwd"})
# Linux prctl option that makes a process adopt the orphans of its descendants.
_PR_SET_CHILD_SUBREAPER = 36
# The shell's exit codes for a command it cannot find or cannot run.
_REAPER_NOT_FOUND = 127
_REAPER_NOT_EXECUTABLE = 126
# The reaper stops sweeping after this many rounds, so a descendant that keeps
# forking cannot hold the session open.
_REAPER_SWEEPS = 50
# After a stop signal, the reaper gives the shell this long to finish its EXIT
# trap, then kills the tree, so a shell that ignores SIGTERM cannot hold the
# session open when the bridge has no timeout.
_REAPER_STOP_GRACE_SECONDS = 5.0
# One check function shared by the Pi extension, the OpenCode plugin and the
# eve sandbox. It asks this module for the verdict, so the matching lives in
# one place.
_GUARD_JS_CHECK = """import {{ execFileSync }} from "node:child_process";

const APP_GUARD_REFUSAL = {refusal};

function appGuardReason(tool, input) {{
  try {{
    const reply = execFileSync({python}, [{bridge}, "guard", "check", {policy}, {digest}], {{
      input: JSON.stringify({{ tool: String(tool || ""), input: input ?? null }}),
      timeout: 10000,
      encoding: "utf8",
    }});
    const verdict = JSON.parse(reply);
    if (!verdict || typeof verdict !== "object" || !("reason" in verdict)) return APP_GUARD_REFUSAL;
    return verdict.reason || null;
  }} catch {{
    // Fail closed: a guard that cannot answer refuses the call.
    return APP_GUARD_REFUSAL;
  }}
}}
"""
_GUARD_PI_EXTENSION = """
export default function (pi) {
  pi.on("tool_call", async (event) => {
    const reason = appGuardReason(event.toolName, event.input);
    return reason ? { block: true, reason } : undefined;
  });
}
"""
# OpenCode loads every export of a plugin file as a plugin, so only the
# plugin itself is exported.
_GUARD_OPENCODE_PLUGIN = """
export const AppGuard = async () => ({
  "tool.execute.before": async (input, output) => {
    const reason = appGuardReason(input.tool, output.args);
    if (reason) throw new Error(reason);
  },
});
"""
# The eve workspace sandbox imports this module and asks the check before each
# tool call, because eve has no hook that the bridge can configure.
_GUARD_EVE_MODULE = """
export { appGuardReason };
"""


def hidden_kind(path: str) -> str:
    """Name the kind of a hidden path for the run log, without its name.

    Args:
        path: Repository-relative hidden path.

    Returns:
        ``"config"`` for an ``ascent.toml`` file, else ``"scoring"``.
    """
    return "config" if PurePosixPath(path).name.casefold() == "ascent.toml" else "scoring"


def _path_hash(path: str) -> str:
    """Hash one normalized repository-relative path.

    Args:
        path: Relative path with ``/`` separators.

    Returns:
        The SHA-256 hex digest.
    """
    # A tool call can carry a lone surrogate, which strict UTF-8 cannot encode.
    return hashlib.sha256(path.encode("utf-8", errors="surrogatepass")).hexdigest()


def hidden_hashes(hidden_paths: Iterable[str], fold_case: bool = False) -> dict[str, str]:
    """Hash the hidden paths for a guard, so the guard never holds their names (E61).

    Args:
        hidden_paths: Repository-relative hidden files and directories.
        fold_case: Whether the workspace's file system ignores case, so that
            ``ASCENT.TOML`` names the same file as ``ascent.toml``.

    Returns:
        Path hashes mapped to their kind.
    """
    hashes = {}
    for path in hidden_paths:
        normalized = os.path.normpath(str(path).strip().strip("/"))
        # Only a ``..`` part leaves the repository: ``..data`` is a plain name.
        if normalized in ("", ".", "..") or normalized.startswith(f"..{os.sep}"):
            continue
        key = normalized.replace(os.sep, "/")
        hashes[_path_hash(key.casefold() if fold_case else key)] = hidden_kind(normalized)
    return hashes


def hashed_kind(hashes: Mapping[str, str], path: str) -> str | None:
    """Return the kind of the hidden path that is ``path`` or holds it, from the hash table only.

    Args:
        hashes: Hashes from :func:`hidden_hashes`, without case folding.
        path: Repository-relative path.

    Returns:
        The hidden kind, or ``None`` when no hidden path covers ``path``.
    """
    normalized = posixpath.normpath(path.strip().strip("/"))
    if normalized in ("", ".", "..") or normalized.startswith("../"):
        return None
    parts = normalized.split("/")
    for end in range(1, len(parts) + 1):
        kind = hashes.get(_path_hash("/".join(parts[:end])))
        if kind:
            return kind
    return None


def guard_policy(
    workspace: Path, hidden_paths: list[str], log: Path, fold_case: bool = False, home: Path | None = None
) -> dict[str, Any]:
    """Build the refusal policy of one session.

    The policy holds hashes, not names, because the agent can read the file
    and must not learn the hidden names from it.

    Args:
        workspace: Directory the agent works in.
        hidden_paths: Repository-relative hidden files and directories.
        log: File each refusal is appended to.
        fold_case: Whether the workspace's file system ignores case, so that
            ``ASCENT.TOML`` names the same file as ``ascent.toml``.
        home: The agent's home folder, for ``~`` and ``$HOME``. The default
            is this process's home folder.

    Returns:
        The policy: workspace, home folder, path hashes mapped to their
        kind, case folding, and log file.
    """
    return _hashed_policy(workspace, hidden_hashes(hidden_paths, fold_case), log, fold_case, home)


def _hashed_policy(
    workspace: Path, hashes: Mapping[str, str], log: Path, fold_case: bool, home: Path | None
) -> dict[str, Any]:
    """Build the refusal policy of one session from path hashes.

    Args:
        workspace: Directory the agent works in.
        hashes: Hashes from :func:`hidden_hashes`.
        log: File each refusal is appended to.
        fold_case: Whether ``hashes`` were made with case folding.
        home: The agent's home folder; the default is this process's home folder.

    Returns:
        The policy that :func:`guard_check` reads.
    """
    return {
        "workspace": str(workspace),
        "home": str(home if home is not None else Path.home()),
        "hashes": dict(hashes),
        "fold_case": fold_case,
        "log": str(log),
    }


def _expand_dirs(text: str, root: str, home: str) -> str:
    """Replace the shell forms of the working and home directories with their paths.

    Args:
        text: One string from a tool call.
        root: The workspace path.
        home: The agent's home folder, or empty when it is not known.

    Returns:
        The text with ``$PWD``, ``${PWD}``, ``$(pwd)`` and backquoted ``pwd``
        replaced, and ``$HOME``, ``${HOME}`` and a leading ``~`` replaced.
    """
    text = _GUARD_PWD_RE.sub(lambda _: root, text)
    return _GUARD_HOME_RE.sub(lambda _: home, text) if home else text


def _dynamic_match(text: str, hashes: dict[str, str], fold: bool) -> str | None:
    """Return the kind of a hidden name that follows a shell expansion the guard cannot resolve.

    The guard resolves ``~/``, ``$HOME``, ``${HOME}``, ``$PWD`` and ``$(pwd)``
    before this check. Any other expansion (another variable, a command
    substitution, ``~+``, ``~-`` or ``~user``) can name any folder. So a hidden
    name after one is refused, whatever folder the expansion names. The shell
    has too many such forms to resolve each one.

    Args:
        text: One string from a tool call, with the known folders expanded.
        hashes: Policy hashes mapped to their kind.
        fold: Whether to compare paths without case.

    Returns:
        The hidden kind, or ``None``.
    """
    for match in _GUARD_WORD_RE.finditer(text):
        run = match.group()
        # Quotes join words in a shell, so ``"$(cmd)"/x`` is one path.
        before = text[: match.start()].rstrip("'\"")
        after_expansion = bool(before) and before[-1] in _GUARD_DYNAMIC_PREFIX
        if not after_expansion and not (run.startswith("~") and not run.startswith("~/")):
            continue
        parts = [part for part in run.split("/") if part not in ("", ".")]
        # The check below is quadratic in the parts of one run.
        if len(parts) > _GUARD_MAX_PARTS:
            raise ValueError("path after an expansion too deep to check")
        for start in range(len(parts)):
            for end in range(start + 1, len(parts) + 1):
                key = "/".join(parts[start:end])
                kind = hashes.get(_path_hash(key.casefold() if fold else key))
                if kind:
                    return kind
    return None


class _LinkResolver:
    """Resolve symbolic links like ``os.path.realpath``, with a cap on the work.

    ``os.path.realpath`` follows every link of a chain each time it is called.
    The agent can make long chains, so a call with many candidates could keep
    the guard past the hook timeout. This resolver keeps each link's result and
    refuses when the call needs too many path steps or runs past its deadline.
    """

    def __init__(self, deadline: float) -> None:
        """Start with no path parts read.

        Args:
            deadline: The ``time.monotonic()`` value after which the resolver refuses.
        """
        self._paths: dict[str, str] = {}
        self._steps_left = _GUARD_MAX_LINK_STEPS
        self._deadline = deadline

    def resolve(self, path: str) -> str:
        """Return the real form of an absolute path.

        Args:
            path: An absolute, normalized path.

        Returns:
            The path with every symbolic link resolved. A part that does not
            exist stays as it is, as in ``os.path.realpath``.

        Raises:
            ValueError: When the path needs too many steps, a chain is longer
                than the system allows, or the deadline passes.
        """
        return self._walk("/", path, 0)

    def _walk(self, start: str, path: str, depth: int) -> str:
        """Resolve each part of a path, from a real start folder.

        Args:
            start: The real folder a relative ``path`` starts in.
            path: The path to resolve.
            depth: How many links lead to this path.

        Returns:
            The resolved path.

        Raises:
            ValueError: When the call needs too many steps, or the deadline passes.
        """
        resolved = "/" if path.startswith("/") else start
        for part in path.split("/"):
            self._steps_left -= 1
            if self._steps_left < 0:
                raise ValueError("too many symbolic links to check")
            if part in ("", "."):
                continue
            if part == "..":
                resolved = str(Path(resolved).parent)
                continue
            resolved = self._follow(str(Path(resolved, part)), depth)
        return resolved

    def _follow(self, path: str, depth: int) -> str:
        """Return the real form of one path whose parent is already real.

        Args:
            path: The path to resolve.
            depth: How many links lead to this path.

        Returns:
            The link's resolved target, or ``path`` when it is not a link.

        Raises:
            ValueError: When the call needs too many steps, a chain is longer
                than the system allows, or the deadline passes.
        """
        known = self._paths.get(path)
        if known is not None:
            return known
        if time.monotonic() > self._deadline:
            raise ValueError("tool call took too long to check")
        try:
            target = str(Path(path).readlink())
        except OSError:
            self._paths[path] = path
            return path
        if depth >= _GUARD_MAX_LINK_DEPTH:
            raise ValueError("too many symbolic links to check")
        result = self._walk(str(Path(path).parent), target, depth + 1)
        self._paths[path] = result
        return result


def _hidden_match(
    text: str, roots: list[str], bases: list[str], hashes: dict[str, str], fold: bool, links: _LinkResolver
) -> str | None:
    """Return the kind of the hidden path one candidate names, if any.

    Matching is exact: the candidate, or one of its parent directories, must
    be a hidden path. A glob such as ``**/*`` names no hidden path. The
    candidate is resolved against every directory the call can work in, and
    symbolic links are followed, so an alias of the workspace still matches.

    Args:
        text: One path-like string from a tool call.
        roots: The workspace path and its resolved form.
        bases: Directories a relative candidate can be relative to.
        hashes: Policy hashes mapped to their kind.
        fold: Whether to compare paths without case.
        links: The call's link resolver.

    Returns:
        The hidden kind, or ``None``.
    """
    candidate = text.strip().strip("'\"`").removeprefix("file://")
    if not candidate:
        return None
    for base in bases:
        path = os.path.normpath(Path(base, candidate))
        for full in dict.fromkeys((path, links.resolve(path))):
            for root in roots:
                # On a disk that ignores case, /USERS/... names the same file as /Users/....
                relative = os.path.relpath(full.casefold(), root.casefold()) if fold else os.path.relpath(full, root)
                if relative in (".", "..") or relative.startswith(f"..{os.sep}"):
                    continue
                parts = PurePosixPath(Path(relative).as_posix()).parts
                for end in range(1, len(parts) + 1):
                    kind = hashes.get(_path_hash("/".join(parts[:end])))
                    if kind:
                        return kind
    return None


def _shell_words(command: str, bases: list[str], home: str = "", depth: int = 0) -> Iterator[str]:
    """Yield every word, and every path-like run inside a word, of a shell command.

    Args:
        command: Shell command text.
        bases: Receives the directory of each ``cd`` or ``pushd``, and of each
            folder option such as ``env -C`` or ``git -C``, in the command.
        home: The agent's home folder, where a ``cd`` with no folder goes.
        depth: Nesting level, for quoted scripts such as ``bash -lc '...'``.

    Yields:
        Candidate path strings.
    """
    try:
        lexer = shlex.shlex(command, posix=True, punctuation_chars=True)
        lexer.whitespace_split = True
        words = list(lexer)
    except ValueError:
        words = command.split()
    after_cd = after_dir_option = False
    for word in words:
        is_operator = bool(word) and all(character in "();<>|&" for character in word)
        # ``grep -C 3`` gives a count, not a folder.
        if after_dir_option and not is_operator and not word.isdigit():
            bases.append(word)
        after_dir_option = word in _GUARD_DIR_OPTIONS
        option, _, value = word.partition("=")
        if option in _GUARD_DIR_OPTIONS and value:
            bases.append(value)
        elif word.startswith("-C") and len(word) > 2 and not word.startswith("--") and not word[2:].isdigit():
            bases.append(word[2:])
        # ``cd -P dir`` and ``cd -- dir`` work in ``dir``; a lone ``-`` is the old folder.
        if after_cd and not is_operator and (word == "-" or not word.startswith("-")):
            bases.append(word)
            after_cd = False
        elif is_operator:
            # A ``cd`` with no folder goes to the home folder.
            if after_cd and home:
                bases.append(home)
            after_cd = False
        elif word in _GUARD_CD_WORDS:
            after_cd = True
        yield word
        yield from _GUARD_WORD_RE.findall(word)
        if depth < _GUARD_SHELL_DEPTH and any(character.isspace() for character in word):
            yield from _shell_words(word, bases, home, depth + 1)
    if after_cd and home:
        bases.append(home)


def _guard_candidates(
    tool: str, tool_input: Any, root: str, home: str, bases: list[str], texts: list[str]
) -> Iterator[str]:
    """Yield the path-like strings of one tool call.

    Args:
        tool: The harness's tool name.
        tool_input: The tool's arguments.
        root: The workspace path, for ``$PWD`` and its other forms.
        home: The agent's home folder, for ``~`` and ``$HOME``.
        bases: Receives the working directories the call names.
        texts: Receives each expanded command, path and directory string.

    Yields:
        Candidate path strings.
    """
    stack: list[tuple[str, Any]] = [("", tool_input)]
    while stack:
        key, value = stack.pop()
        name = key.lower()
        if isinstance(value, dict):
            stack.extend((str(child), item) for child, item in value.items())
            continue
        if isinstance(value, list):
            stack.extend((key, item) for item in value)
            continue
        if not isinstance(value, str):
            continue
        value = _expand_dirs(value, root, home)
        is_grep_pattern = name == "pattern" and "grep" in tool.lower()
        if not is_grep_pattern and (
            name in _GUARD_COMMAND_KEYS | _GUARD_BASE_KEYS | _GUARD_PATH_KEYS or "path" in name or "file" in name
        ):
            texts.append(value)
            # Checked before the shell parse, which is the slow step on a long command.
            if sum(map(len, texts)) > _GUARD_MAX_TEXT:
                raise ValueError("tool call too long to check")
        if "*** Begin Patch" in value:
            yield from _GUARD_PATCH_RE.findall(value)
        if name in _GUARD_COMMAND_KEYS:
            yield from _shell_words(value, bases, home)
        elif name in _GUARD_BASE_KEYS:
            bases.append(value)
            yield value
        elif "path" in name or "file" in name or name in _GUARD_PATH_KEYS:
            # A grep pattern is a regex over file contents, not a path.
            if is_grep_pattern:
                continue
            yield value


def guard_check(policy: dict[str, Any], tool: str, tool_input: Any, cwd: str | None = None) -> str | None:
    """Tell whether one tool call names a hidden path.

    Args:
        policy: Policy from :func:`guard_policy`.
        tool: The harness's tool name.
        tool_input: The tool's arguments.
        cwd: The folder the call runs in, when the harness reports it. A shell
            that keeps its folder between calls runs a call after ``cd eval``
            in ``eval``.

    Returns:
        The hidden kind the call names, or ``None`` to let it run.

    Raises:
        ValueError: When the policy has no hashes, or the call is above a
            cost cap, so the guard cannot answer in time.
    """
    hashes = policy.get("hashes")
    # A session gets a guard only when it has hidden paths, so an empty table is a broken policy.
    if not isinstance(hashes, dict) or not hashes:
        raise ValueError("guard policy has no hashes")
    workspace = str(policy.get("workspace") or Path.cwd())
    roots = list(dict.fromkeys([workspace, os.path.realpath(workspace)]))
    # The call's folder is exact, so it stays out of the cap on named folders.
    start = [os.path.normpath(Path(roots[0], cwd))] if cwd else []
    named: list[str] = []
    texts: list[str] = []
    home = str(policy.get("home") or "")
    candidates = list(_guard_candidates(tool, tool_input, (start or roots)[0], home, named, texts))
    fold = bool(policy.get("fold_case"))
    for text in texts:
        kind = _dynamic_match(text, hashes, fold)
        if kind:
            return kind
    if len(named) > _GUARD_MAX_BASES:
        raise ValueError("too many directory changes to check")
    # A later cd is relative to an earlier one, so each one extends every known directory.
    bases = list(dict.fromkeys(roots + start))
    for directory in named:
        bases += [os.path.normpath(Path(base, directory)) for base in bases]
        bases = list(dict.fromkeys(bases))
    candidates = list(dict.fromkeys(candidates))
    if len(bases) * sum(1 + candidate.count("/") for candidate in candidates) > _GUARD_MAX_CHECKS:
        raise ValueError("tool call too large to check")
    # A backstop for a cost the caps above miss, such as a slow file system.
    deadline = time.monotonic() + _GUARD_DEADLINE_SECONDS
    links = _LinkResolver(deadline)
    for candidate in candidates:
        if time.monotonic() > deadline:
            raise ValueError("tool call took too long to check")
        kind = _hidden_match(candidate, roots, bases, hashes, fold, links)
        if kind:
            return kind
    return None


def guard_main(argv: list[str]) -> int:
    """Answer one tool-call check from a harness hook.

    ``hook`` reads a Codex or Claude ``PreToolUse`` event, which also gives
    the call's folder as ``cwd``, and prints its deny answer.
    ``check`` reads ``{"tool", "input"}`` and prints ``{"reason"}``, with a
    ``null`` reason to let the call run. Each refusal appends only the tool
    and the path kind to the policy log. A guard that cannot answer (a
    missing, changed or broken policy, unreadable input) refuses the call.

    Args:
        argv: ``[mode, policy_file, digest]``, where ``digest`` is the
            SHA-256 of the policy file as the session wrote it.

    Returns:
        The exit code: 0, or 2 for a usage error.
    """
    if len(argv) != 3 or argv[0] not in ("hook", "check"):
        print("usage: harness_bridge.py guard <hook|check> <policy.json> <sha256>", file=sys.stderr)
        return 2
    mode, policy_file, digest = argv
    tool, kind = "", None
    try:
        raw = Path(policy_file).read_bytes()
        # The agent can write the session folder; a policy it rewrote must not answer.
        if hashlib.sha256(raw).hexdigest() != digest:
            raise ValueError("guard policy changed")
        policy = json.loads(raw)
        request = json.loads(sys.stdin.read() or "{}")
        if mode == "hook":
            tool, tool_input = str(request.get("tool_name") or ""), request.get("tool_input")
            cwd = request.get("cwd") if isinstance(request.get("cwd"), str) else None
        else:
            tool, tool_input, cwd = str(request.get("tool") or ""), request.get("input"), None
        kind = guard_check(policy, tool, tool_input, cwd)
        if kind and policy.get("log"):
            with Path(policy["log"]).open("a", encoding="utf-8") as handle:
                handle.write(json.dumps({"tool": tool, "kind": kind}) + "\n")
    # Every failure refuses: a crashed hook lets the call run.
    except Exception as error:
        print(f"guard error: {type(error).__name__}", file=sys.stderr)
        kind = kind or "unknown"
    if mode == "check":
        print(json.dumps({"reason": GUARD_REFUSAL if kind else None}))
    elif kind:
        decision = {
            "hookEventName": "PreToolUse",
            "permissionDecision": "deny",
            "permissionDecisionReason": GUARD_REFUSAL,
        }
        print(json.dumps({"hookSpecificOutput": decision}))
    return 0


def write_guard_policy(policy_file: Path, policy: dict[str, Any]) -> str:
    """Write one session's guard policy and return the digest its hooks check.

    Args:
        policy_file: Where to write the policy.
        policy: Policy from :func:`guard_policy`.

    Returns:
        The SHA-256 hex digest of the written file.
    """
    raw = json.dumps(policy).encode("utf-8")
    policy_file.write_bytes(raw)
    return hashlib.sha256(raw).hexdigest()


def guard_command(mode: str, policy_file: Path, digest: str) -> list[str]:
    """Build the command line that runs one guard check.

    Args:
        mode: ``"hook"`` or ``"check"``.
        policy_file: The session's policy file.
        digest: The policy digest from :func:`write_guard_policy`.

    Returns:
        The argument list.
    """
    return [sys.executable, str(Path(__file__).resolve()), "guard", mode, str(policy_file), digest]


def codex_hooks(policy_file: Path, digest: str) -> dict[str, Any]:
    """Build a Codex ``hooks.json`` that sends every tool call through the guard.

    Args:
        policy_file: The session's policy file.
        digest: The policy digest from :func:`write_guard_policy`.

    Returns:
        The hooks document.
    """
    hook = {"type": "command", "command": shlex.join(guard_command("hook", policy_file, digest)), "timeout": 10}
    return {"hooks": {"PreToolUse": [{"hooks": [hook]}]}}


def install_guard(
    session_dir: Path, workspace: Path, hashes: Mapping[str, str], home: Path | None = None
) -> dict[str, str]:
    """Write the refusal hooks of every harness for one session.

    The guest gets only the hashes, so no file or variable the agent can read
    names a hidden path (E61).

    Args:
        session_dir: Private directory of the session.
        workspace: Directory the agent works in.
        hashes: Hidden path hashes from :func:`hidden_hashes`, made by the parent without case folding.
        home: The agent's home folder; the default is this process's home folder.

    Returns:
        The environment that points each harness at its hook.
    """
    guard_dir = session_dir / GUARD_DIR
    (guard_dir / "plugin").mkdir(parents=True, exist_ok=True)
    log = guard_dir / "refusals.jsonl"
    log.write_text("", encoding="utf-8")
    policy_file = guard_dir / "policy.json"
    digest = write_guard_policy(policy_file, _hashed_policy(workspace, hashes, log, False, home))
    python, bridge, _, _, policy, _ = guard_command("check", policy_file, digest)
    check = _GUARD_JS_CHECK.format(
        refusal=json.dumps(GUARD_REFUSAL),
        python=json.dumps(python),
        bridge=json.dumps(bridge),
        policy=json.dumps(policy),
        digest=json.dumps(digest),
    )
    pi_extension = guard_dir / "pi-guard.ts"
    pi_extension.write_text(check + _GUARD_PI_EXTENSION, encoding="utf-8")
    (guard_dir / "plugin" / "app-guard.js").write_text(check + _GUARD_OPENCODE_PLUGIN, encoding="utf-8")
    eve_module = guard_dir / "eve-guard.mjs"
    eve_module.write_text(check + _GUARD_EVE_MODULE, encoding="utf-8")
    codex_file = guard_dir / "codex-hooks.json"
    codex_file.write_text(json.dumps(codex_hooks(policy_file, digest)), encoding="utf-8")
    return {
        "APP_GUARD_PI": str(pi_extension),
        "APP_GUARD_CODEX": str(codex_file),
        "APP_GUARD_EVE": str(eve_module),
        "OPENCODE_CONFIG_DIR": str(guard_dir),
    }


def read_refusals(session_dir: Path) -> list[dict[str, str]]:
    """Read the refusals one session logged.

    The agent can write the log, so a line that is not a JSON object with a
    string ``tool`` and ``kind`` is skipped; it must not break the session's
    result or settlement.

    Args:
        session_dir: Private directory of the session.

    Returns:
        One ``{"tool", "kind"}`` entry per well-formed refusal line.
    """
    log = session_dir / GUARD_DIR / "refusals.jsonl"
    if not log.exists():
        return []
    refusals = []
    for line in log.read_text(encoding="utf-8", errors="replace").splitlines():
        try:
            entry = json.loads(line)
        # Deeply nested brackets make the parser raise RecursionError, not ValueError.
        except (ValueError, RecursionError):
            continue
        if isinstance(entry, dict) and isinstance(entry.get("tool"), str) and isinstance(entry.get("kind"), str):
            refusals.append({"tool": entry["tool"], "kind": entry["kind"]})
    return refusals


def refusal_record(refusal: dict[str, Any]) -> dict[str, Any]:
    """Build the run-log record a sandboxed engine sends for one hidden-path refusal.

    Only the tool and the path kind travel, never the path or its content.

    Args:
        refusal: One ``{"tool", "kind"}`` entry of a session.

    Returns:
        The record the parent forwards to the run log as a ``proposer.refusal`` event.
    """
    tool, kind = str(refusal.get("tool") or ""), str(refusal.get("kind") or "")
    return {
        "level": "WARNING",
        "logger": "app.engine",
        "message": f"Refused a {tool} call on a hidden {kind} path; the session continues",
        "source": "proposer",
        "event": "proposer.refusal",
        "fields": {"tool": tool, "kind": kind},
    }


@dataclass
class SessionOutcome:
    """What one harness session produced."""

    text: str | None
    stdout: str
    stderr: str
    returncode: int
    usage: Usage
    cost_usd: float
    duration_seconds: float
    timed_out: bool = False
    budget_exceeded: bool = False
    tool_calls: int = 0
    tool_cap_reached: bool = False
    refusals: list[dict[str, str]] = field(default_factory=list)

    @property
    def failed(self) -> bool:
        """Tell whether the session ended in an error rather than a finish or a tool-cap stop.

        Returns:
            True for a timeout, or a non-zero exit the tool cap did not cause.
        """
        return self.timed_out or (self.returncode != 0 and not self.tool_cap_reached)


@dataclass
class _Monitor:
    """Stream the harness output, stopping it once the session budget or tool-call cap is spent."""

    process: subprocess.Popen[str]
    output_format: str
    price: dict[str, float] | None
    max_cost_usd: float | None
    max_tool_calls: int | None = None
    root_start: int | None = None
    chunks: list[str] = field(default_factory=list)
    budget_exceeded: bool = False
    tool_calls: int = 0
    tool_cap_reached: bool = False
    _seen_calls: set[str] = field(default_factory=set)

    @property
    def stopped(self) -> bool:
        """Tell whether the monitor itself stopped the harness.

        Returns:
            True once the budget or the tool-call cap was exceeded.
        """
        return self.budget_exceeded or self.tool_cap_reached

    def pump(self) -> None:
        """Read stdout to completion, re-pricing and counting tool calls as events arrive."""
        assert self.process.stdout is not None
        for line in self.process.stdout:
            self.chunks.append(line)
            if self.max_tool_calls is not None:
                self._count_tool_calls(line)
            if self.max_cost_usd is None or not self.price or "usage" not in line:
                continue
            _, usage = parse_output(self.output_format, "".join(self.chunks))
            if cost_usd(usage, self.price) > self.max_cost_usd:
                self.budget_exceeded = True
                _stop_group(self.process, signal.SIGTERM, self.root_start)

    def _count_tool_calls(self, line: str) -> None:
        """Count the tool calls one output line starts and stop the harness past the cap.

        Args:
            line: One line of harness stdout.
        """
        for event in _json_lines(line):
            for call_id in tool_call_ids(self.output_format, event):
                if call_id and call_id in self._seen_calls:
                    continue
                if call_id:
                    self._seen_calls.add(call_id)
                self.tool_calls += 1
        if self.max_tool_calls is not None and self.tool_calls > self.max_tool_calls and not self.tool_cap_reached:
            self.tool_cap_reached = True
            _stop_group(self.process, signal.SIGTERM, self.root_start)


def _stop_group(process: subprocess.Popen[Any], signum: int, root_start: int | None) -> None:
    """Signal the harness shell and every child it spawned, once each.

    Args:
        process: The shell running the harness command, started in its own session.
        signum: Signal to deliver to the process group and to every descendant outside it.
        root_start: Start time of the shell from ``/proc``, or ``None`` where ``/proc`` is absent.
    """
    stats = _proc_stats() if root_start is not None else None
    if stats is not None and process.pid in stats and stats[process.pid][2] != root_start:
        # The shell was reaped and an unrelated process took its pid. That
        # process can lead its own group, so neither the group nor the tree is ours.
        return
    # eve's sandbox starts each tool command detached, in its own session and
    # group, so the group signal misses it. The tree is read first: once a
    # parent dies, its children move to init and leave the tree.
    descendants = []
    if stats and process.pid in stats:
        # killpg already reaches the members of the shell's group.
        descendants = [pid for pid in _descendants(stats, process.pid) if stats[pid][1] != process.pid]
    # Signalling only the shell would leave a child holding the stdout pipe open.
    # macOS answers EPERM, not ESRCH, when the group holds only an unreaped leader.
    with contextlib.suppress(ProcessLookupError, PermissionError):
        os.killpg(process.pid, signum)
    for pid in descendants:
        with contextlib.suppress(OSError):
            os.kill(pid, signum)


def _stat_fields(pid: int | str) -> list[str]:
    """Read the ``/proc/<pid>/stat`` fields that follow the command name.

    Args:
        pid: Process id.

    Returns:
        Fields 3 onward: state, parent pid, process group, and so on; the start time is index 19.
    """
    # The command name in field 2 can hold spaces and parentheses, so the
    # fields are read after its closing parenthesis.
    return Path(f"/proc/{pid}/stat").read_text().rsplit(")", 1)[1].split()


def _start_time(pid: int) -> int | None:
    """Return the start time of a process, which tells it apart from a later process with the same pid.

    Args:
        pid: Process id.

    Returns:
        The start time in clock ticks after boot, or ``None`` where ``/proc`` is absent (macOS).
    """
    try:
        return int(_stat_fields(pid)[19])
    except (OSError, IndexError, ValueError):
        return None


def _proc_stats() -> dict[int, tuple[int, int, int]] | None:
    """Read the parent pid, process group and start time of every process.

    Returns:
        ``(parent, group, start)`` for each pid, or ``None`` where ``/proc`` is absent (macOS).
    """
    try:
        entries = list(Path("/proc").iterdir())
    except FileNotFoundError:
        return None
    stats: dict[int, tuple[int, int, int]] = {}
    for entry in entries:
        if not entry.name.isdigit():
            continue
        try:
            fields = _stat_fields(entry.name)
            stats[int(entry.name)] = (int(fields[1]), int(fields[2]), int(fields[19]))
        except (OSError, IndexError, ValueError):
            continue
    return stats


def _descendants(stats: dict[int, tuple[int, int, int]], root: int) -> list[int]:
    """List the descendants of a process.

    Args:
        stats: The process table from ``_proc_stats``.
        root: Pid whose tree to walk.

    Returns:
        The pids under ``root`` at any depth.
    """
    children: dict[int, list[int]] = {}
    for pid, (parent, _, _) in stats.items():
        children.setdefault(parent, []).append(pid)
    found: list[int] = []
    pending = [root]
    while pending:
        for child in children.get(pending.pop(), []):
            found.append(child)
            pending.append(child)
    return found


def reaper_main(command: list[str]) -> int:
    """Run the harness command as a child subreaper, then kill every process it left behind.

    The bridge starts the harness shell through this entry point on Linux. A
    background job that outlives its parent, for example ``sleep 300 &`` in a
    detached eve tool command, moves to this process instead of to init, so
    the bridge's stop sweep finds it in the tree, and this process kills it
    when the shell ends normally. The native runtime runs many sessions in one
    box, so a leftover job would otherwise outlive its session.

    Args:
        command: The harness shell and its arguments.

    Returns:
        The shell's exit code, or 128 plus the signal number when a signal ended it,
        or 127 (no such file) or 126 (cannot run it) when the command cannot start.
    """
    ctypes.CDLL(None, use_errno=True).prctl(_PR_SET_CHILD_SUBREAPER, 1, 0, 0, 0)
    # restore_signals (the default) puts SIGPIPE and SIGXFSZ back to their
    # default action, which CPython ignores; an ignored SIGPIPE would leave
    # ``producer | head -1`` writing forever.
    # The bridge's stop signals the whole group, this process included. Wait
    # for the shell instead of dying, so the bridge's wait still covers the
    # shell's EXIT trap. A Python handler, unlike SIG_IGN, resets to the
    # default in the shell at exec.
    for signum in (signal.SIGTERM, signal.SIGHUP, signal.SIGINT):
        signal.signal(signum, _arm_stop_grace)
    signal.signal(signal.SIGALRM, _kill_descendants)
    try:
        child = subprocess.Popen(command, restore_signals=True)
    except OSError as error:
        # The caller reads these shell codes as "could not start"; a traceback would hide the one-line reason.
        print(error, file=sys.stderr, flush=True)
        return _REAPER_NOT_FOUND if isinstance(error, FileNotFoundError) else _REAPER_NOT_EXECUTABLE
    returncode = child.wait()
    for _ in range(_REAPER_SWEEPS):
        left = _descendants(_proc_stats() or {}, os.getpid())
        if not left:
            break
        for pid in left:
            with contextlib.suppress(OSError):
                os.kill(pid, signal.SIGKILL)
        _reap_children()
        time.sleep(0.05)
    _reap_children()
    return returncode if returncode >= 0 else 128 - returncode


def _arm_stop_grace(*_: Any) -> None:
    """Start the reaper's stop grace once; ``_kill_descendants`` runs when it ends.

    Args:
        _: The signal number and frame, which the handler ignores.
    """
    if signal.getitimer(signal.ITIMER_REAL)[0] == 0:
        signal.setitimer(signal.ITIMER_REAL, _REAPER_STOP_GRACE_SECONDS)


def _kill_descendants(*_: Any) -> None:
    """Kill every descendant of this process, so the reaper's wait for the shell ends.

    Args:
        _: The timer or signal values, which the handler ignores.
    """
    for pid in _descendants(_proc_stats() or {}, os.getpid()):
        with contextlib.suppress(OSError):
            os.kill(pid, signal.SIGKILL)


def _reap_children() -> None:
    """Collect every finished child, including the orphans this subreaper adopted."""
    while True:
        try:
            pid, _ = os.waitpid(-1, os.WNOHANG)
        except ChildProcessError:
            return
        if pid == 0:
            return


def run_session(
    proposer: dict[str, Any],
    *,
    workspace: Path,
    prompt: str,
    model: str,
    session_dir: Path,
    max_cost_usd: float | None = None,
    timeout_seconds: float | None = None,
    extra_env: dict[str, str] | None = None,
    api_key: str | None = None,
) -> SessionOutcome:
    """Run one proposer session through the configured harness.

    Args:
        proposer: Serialized launch: ``run_command``, ``files``, ``env``,
            ``output_format``, ``instructions_file`` and optional ``price``
            and ``max_tool_calls`` (stop the harness once it starts more
            tool calls than this; its workspace edits so far stand),
            ``hidden_hashes`` (the parent's :func:`hidden_hashes` of the
            scoring-side paths; every tool call that names one is refused),
            ``exec_commands`` (command names the exec shim records), and
            ``resume_command`` (:func:`main` runs it in place of
            ``run_command`` for a ``--resume`` of a known session, so a
            harness with native resume continues its own session state;
            never for a ``file_command``),
            ``setup`` (the contract file's ``[proposer] setup``, run by
            :func:`ensure_setup` before a session in a checkout) and
            ``file_command`` (``run_command`` is the contract file's exact
            ``[proposer] entry``, so no key token is filled into it) and
            ``outside_checkout`` (the default agent's launch, which answers
            a ``file_command`` session in a folder that is not a checkout).
        workspace: Directory the agent works in (the upstream work dir).
        prompt: Task prompt for this session.
        model: Model identifier the harness should route to.
        session_dir: Private directory for prompt, transcript and logs.
        max_cost_usd: Stop the harness once its priced usage exceeds this.
        timeout_seconds: Kill the harness after this many seconds.
        extra_env: Environment overrides layered on top of the launch env.
        api_key: Route key substituted for the launch's key token; defaults
            to the proposer key in ``ASCENT_MODEL_KEY``.

    Returns:
        The parsed answer, raw transcript, usage and exit status.

    Raises:
        RuntimeError: When the setup fails or uses up the session's time, a ``file_command`` session
            outside a checkout has no ``outside_checkout`` launch, or the launch holds a key token from
            before SKY-776.
    """
    workspace.mkdir(parents=True, exist_ok=True)
    session_dir.mkdir(parents=True, exist_ok=True)
    if proposer.get("file_command") and not (workspace / ".git").exists():
        # The file's command and its setup need the repository checkout. An
        # AutoSaddler reflect session runs in a plain folder and edits no
        # repository, so the default agent answers it (SKY-753).
        fallback = proposer.get("outside_checkout")
        if not fallback:
            raise RuntimeError(
                f"The [proposer] entry command in ascent.toml runs only in a repository checkout, not in {workspace}."
            )
        proposer = {**proposer, **fallback, "file_command": False, "setup": None}
    _refuse_old_key_token(proposer)
    if api_key is None:
        api_key = _env(KEY_ENV)
    env = {
        **os.environ,
        **{name: _with_key(value, api_key) for name, value in (proposer.get("env") or {}).items()},
        **(extra_env or {}),
    }
    hashes = dict(proposer.get("hidden_hashes") or {})
    if hashes:
        home = Path(env["HOME"]) if env.get("HOME") else None
        env.update(install_guard(session_dir, workspace, hashes, home))
    if proposer.get("setup"):
        # The setup counts inside the session's time limit. An in-process
        # caller (AutoSaddler) installs no SIGTERM handler; its stop kills
        # every process it started, this setup included.
        setup_started = time.monotonic()
        setup_seconds = (
            SETUP_TIMEOUT_SECONDS if timeout_seconds is None else min(SETUP_TIMEOUT_SECONDS, timeout_seconds)
        )
        # The setup gets the proposer command's environment and guard. The exec shim comes after it, so
        # the exec log holds only the proposer's own commands.
        ensure_setup(list(proposer["setup"]), workspace, setup_seconds, env)
        if timeout_seconds is not None:
            limit = timeout_seconds
            timeout_seconds -= time.monotonic() - setup_started
            if timeout_seconds <= 0:
                # A harness with no time left would be killed at once, so the setup's overrun stops the session.
                raise RuntimeError(
                    f"The [proposer] setup command in ascent.toml ({shlex.join(proposer['setup'])}) used up"
                    f" the session's time limit of {limit:g} seconds."
                )
    for relative_path, content in (proposer.get("files") or {}).items():
        target = contained_path(workspace, relative_path)
        target.parent.mkdir(parents=True, exist_ok=True)
        target.write_text(_with_key(content, api_key), encoding="utf-8")
    instructions = proposer.get("instructions_file")
    if instructions and instructions != "CLAUDE.md":
        pointer = contained_path(workspace, instructions)
        if not pointer.exists():
            pointer.write_text(_POINTER_NOTE, encoding="utf-8")
    attempt = len(list(session_dir.glob("prompt-*.md"))) + 1
    prompt_file = session_dir / f"prompt-{attempt}.md"
    prompt_file.write_text(prompt, encoding="utf-8")
    exec_commands = proposer.get("exec_commands") or []
    if exec_commands:
        shim_dir = install_exec_shim(session_dir / EXEC_SHIM_DIR, exec_commands, sys.executable)
        env["PATH"] = f"{shim_dir}{os.pathsep}{env.get('PATH', '')}"
        env["APP_EXEC_LOG"] = str(session_dir / EXEC_LOG)
    env["ASCENT_MODEL"] = model
    for name, value in (
        ("ANSWER_FILE", str(session_dir / f"answer-{attempt}.txt")),
        # A harness with native resume keeps its own session state here, so the committed tree carries it.
        ("SESSION_DIR", str(session_dir)),
    ):
        env[f"APP_{name}"] = env[_LEGACY_PREFIX + name] = value
    env[TASK_ENV] = str(prompt_file.resolve())
    shell = shutil.which("bash") or "/bin/sh"
    run_command = proposer["run_command"]
    # The file's command is the user's own text: a key token in it must stay text, not become the relay key.
    command = [shell, "-c", run_command if proposer.get("file_command") else _with_key(run_command, api_key)]
    if sys.platform == "linux" and sys.executable:
        command = [sys.executable, "-I", str(Path(__file__).resolve()), "reaper", *command]
    started = time.monotonic()
    stderr_file = session_dir / f"run-{attempt}.stderr"
    with stderr_file.open("w", encoding="utf-8") as stderr:
        with _defer_stop():
            process = subprocess.Popen(
                command,
                cwd=workspace,
                env=env,
                stdin=subprocess.DEVNULL,
                stdout=subprocess.PIPE,
                stderr=stderr,
                text=True,
                errors="replace",
                start_new_session=True,
            )
            max_tool_calls = proposer.get("max_tool_calls")
            monitor = _Monitor(
                process,
                proposer.get("output_format", "plain"),
                proposer.get("price"),
                max_cost_usd,
                int(max_tool_calls) if max_tool_calls else None,
                _start_time(process.pid),
            )
            _OWNED_GROUPS[process.pid] = (process, monitor.root_start)
        try:
            reader = threading.Thread(target=monitor.pump, daemon=True)
            reader.start()
            timed_out = False
            try:
                process.wait(timeout=timeout_seconds)
            except subprocess.TimeoutExpired:
                timed_out = True
                _stop_group(process, signal.SIGKILL, monitor.root_start)
                process.wait()
            else:
                # A job the shell left in its group would outlive the session, and
                # the native runtime runs many sessions in one box. Without /proc
                # (macOS) nothing proves the reaped pid is still ours, so skip it.
                if monitor.root_start is not None:
                    _stop_group(process, signal.SIGKILL, monitor.root_start)
            # A child forked while the first signal landed can outlive the shell and
            # keep the pipe open, so keep sweeping the group until the reader sees EOF.
            deadline = time.monotonic() + 5.0
            while (timed_out or monitor.stopped) and reader.is_alive() and time.monotonic() < deadline:
                _stop_group(process, signal.SIGKILL, monitor.root_start)
                reader.join(timeout=0.2)
            reader.join(timeout=30)
        finally:
            _OWNED_GROUPS.pop(process.pid, None)
    stdout = "".join(monitor.chunks)
    (session_dir / f"run-{attempt}.stdout").write_text(stdout, encoding="utf-8")
    output_format = proposer.get("output_format", "plain")
    text, usage = parse_output(output_format, stdout)
    refusals = read_refusals(session_dir) if hashes else []
    if output_format == "host_agent":
        refusals += parse_host_agent_refusals(stdout)
    return SessionOutcome(
        text=text,
        stdout=stdout,
        stderr=stderr_file.read_text(encoding="utf-8", errors="replace"),
        returncode=process.returncode,
        usage=usage,
        cost_usd=cost_usd(usage, proposer.get("price")),
        duration_seconds=time.monotonic() - started,
        timed_out=timed_out,
        budget_exceeded=monitor.budget_exceeded,
        tool_calls=monitor.tool_calls,
        tool_cap_reached=monitor.tool_cap_reached,
        refusals=refusals,
    )


def claude_project_slug(cwd: Path) -> str:
    """Mirror how the Claude CLI names a project's transcript directory.

    Args:
        cwd: Working directory of the session.

    Returns:
        The directory name under ``~/.claude/projects``.
    """
    return _SLUG_RE.sub("-", str(cwd.resolve()))


def record_transcript(session_id: str, workspace: Path, model: str, usage: Usage) -> None:
    """Append a Claude-shaped transcript line so upstream usage readers find the session.

    Args:
        session_id: Session identifier the upstream engine assigned.
        workspace: Working directory of the session.
        model: Model the harness ran on.
        usage: Token counts of this invocation.
    """
    project = Path.home() / ".claude" / "projects" / claude_project_slug(workspace)
    project.mkdir(parents=True, exist_ok=True)
    entry = {
        "type": "assistant",
        "sessionId": session_id,
        "message": {
            "id": uuid.uuid4().hex,
            "model": model,
            "usage": {
                "input_tokens": usage.get("input_tokens", 0),
                "output_tokens": usage.get("output_tokens", 0),
                "cache_read_input_tokens": 0,
                "cache_creation_input_tokens": 0,
            },
        },
    }
    with (project / f"{session_id}.jsonl").open("a", encoding="utf-8") as handle:
        handle.write(json.dumps(entry) + "\n")


def _event_error(event: dict[str, Any]) -> str | None:
    """Return the message of a harness event that reports an error, if it is one.

    Args:
        event: One parsed JSON event from the harness stdout.

    Returns:
        The error message, or ``None`` for ordinary events.
    """
    kind = str(event.get("type", "")).lower()
    if "error" not in kind and "fail" not in kind and not event.get("is_error"):
        return None
    error = event.get("error")
    message = (
        event.get("message") or (error.get("message") if isinstance(error, dict) else error) or event.get("result")
    )
    return str(message or json.dumps(event))[:500]


def failure_detail(outcome: SessionOutcome, limit: int = 2000) -> str:
    """Summarize why a harness session failed, from both of its output streams.

    JSON harnesses such as Codex report the real error as an event on stdout and
    only a banner on stderr, so stderr alone would hide the cause.

    Args:
        outcome: Parsed session outcome.
        limit: Maximum length of the summary.

    Returns:
        The stderr tail followed by any error events or plain lines from stdout.
    """
    parts = []
    stderr = outcome.stderr.strip()
    if stderr:
        parts.append(stderr[-limit:])
    errors: list[str] = []
    for event in _json_lines(outcome.stdout):
        message = _event_error(event)
        if message and (not errors or errors[-1] != message):
            errors.append(message)
    plain = [line for line in outcome.stdout.splitlines() if line.strip() and not line.lstrip().startswith("{")]
    stdout_lines = errors[-10:] or plain[-20:]
    if stdout_lines:
        parts.append("stdout: " + "\n".join(stdout_lines))
    return "\n".join(parts)[-limit:]


def result_document(session_id: str, model: str, outcome: SessionOutcome) -> dict[str, Any]:
    """Shape a session outcome like ``claude --print --output-format json``.

    Args:
        session_id: Session identifier reported back to the caller.
        model: Model the harness ran on.
        outcome: Parsed session outcome.

    Returns:
        The result document the upstream engines parse.
    """
    # A tool-cap stop ends the session early on purpose: its workspace edits
    # are the answer, so it reports success even without a final message.
    is_error = outcome.failed or (outcome.text is None and not outcome.tool_cap_reached)
    input_tokens = outcome.usage.get("input_tokens", 0)
    output_tokens = outcome.usage.get("output_tokens", 0)
    if outcome.text is not None:
        result = outcome.text
    elif outcome.tool_cap_reached:
        result = (
            f"Stopped at the session's tool-call cap ({outcome.tool_calls} started); the workspace holds its edits."
        )
    else:
        result = f"Harness exited with {outcome.returncode}: {failure_detail(outcome)}"
    return {
        "type": "result",
        "subtype": "success" if not is_error else "error_during_execution",
        "is_error": is_error,
        "duration_ms": int(outcome.duration_seconds * 1000),
        "num_turns": 1,
        "result": result,
        "session_id": session_id,
        "total_cost_usd": outcome.cost_usd,
        "usage": {
            "input_tokens": input_tokens,
            "output_tokens": output_tokens,
            "cache_read_input_tokens": 0,
            "cache_creation_input_tokens": 0,
        },
        "modelUsage": {
            model: {
                "inputTokens": input_tokens,
                "outputTokens": output_tokens,
                "cacheReadInputTokens": 0,
                "cacheCreationInputTokens": 0,
                "costUSD": outcome.cost_usd,
            }
        },
        "refusals": outcome.refusals,
    }


def _cli_parser() -> argparse.ArgumentParser:
    """Describe the subset of the Claude CLI surface the upstream engines use.

    Returns:
        A parser that tolerates unknown flags and keeps the prompt positional.
    """
    parser = argparse.ArgumentParser(prog="claude", add_help=False, allow_abbrev=False)
    for flag in ("--print", "-p", "--verbose", "--dangerously-skip-permissions", "--continue", "-c"):
        parser.add_argument(flag, action="store_true", dest=flag.strip("-").replace("-", "_"))
    for flag in (
        "--output-format",
        "--input-format",
        "--model",
        "--session-id",
        "--resume",
        "-r",
        "--settings",
        "--permission-mode",
        "--effort",
        "--max-budget-usd",
        "--max-turns",
        "--allowedTools",
        "--disallowedTools",
        "--tools",
        "--add-dir",
        "--append-system-prompt",
        "--system-prompt",
        "--mcp-config",
        "--agents",
    ):
        parser.add_argument(flag, dest=flag.strip("-").replace("-", "_"))
    parser.add_argument("prompt", nargs="*")
    return parser


def install_shim(home: Path, bridge_file: Path, python: str) -> Path:
    """Write a ``claude`` executable that forwards to this module.

    Args:
        home: Sandbox home directory.
        bridge_file: Absolute path of this module inside the sandbox.
        python: Interpreter that runs the bridge.

    Returns:
        The directory that must lead ``PATH``.
    """
    shim_dir = home / SHIM_DIR
    shim_dir.mkdir(parents=True, exist_ok=True)
    shim = shim_dir / "claude"
    shim.write_text(f'#!/bin/sh\nexec "{python}" "{bridge_file}" "$@"\n', encoding="utf-8")
    shim.chmod(0o755)
    return shim_dir


def install_exec_shim(shim_dir: Path, names: Iterable[str], python: str) -> Path:
    """Write one wrapper for each command name that runs it through ``exec_shim.py``.

    Args:
        shim_dir: Folder for the wrappers; it must lead ``PATH``.
        names: Plain command names to record.
        python: Interpreter that runs the shim.

    Returns:
        ``shim_dir``.

    Raises:
        ValueError: When a name is not a plain file name.
        FileNotFoundError: When ``exec_shim.py`` is not next to this file.
    """
    shim_file = Path(__file__).resolve().with_name("exec_shim.py")
    if not shim_file.is_file():
        raise FileNotFoundError(f"exec shim not found: {shim_file}")
    shim_dir.mkdir(parents=True, exist_ok=True)
    for name in names:
        if name in ("", ".", "..") or "/" in name or "\0" in name:
            raise ValueError(f"exec shim command must be a plain name: {name!r}")
        wrapper = shim_dir / name
        wrapper.write_text(
            # -I: a PYTHONPATH or a json.py in the agent's folder must not shadow the shim's stdlib imports.
            f'#!/bin/sh\nexec {shlex.join([python, "-I", str(shim_file), str(shim_dir), name])} "$@"\n',
            encoding="utf-8",
        )
        wrapper.chmod(0o755)
    return shim_dir


def main(argv: list[str]) -> int:
    """Answer one ``claude --print`` invocation with the configured harness.

    Args:
        argv: Command-line arguments after the program name.

    Returns:
        The process exit code: the harness's own code, or 1 for a bridge failure.
    """
    if argv[:1] == ["guard"]:
        return guard_main(argv[1:])
    if argv[:1] == ["reaper"]:
        return reaper_main(argv[1:])
    signal.signal(signal.SIGTERM, _stop_owned_groups)
    args, unknown = _cli_parser().parse_known_args(argv)
    if args.output_format not in (None, "json"):
        print(f"claude bridge: unsupported --output-format {args.output_format}", file=sys.stderr)
        return 1
    if unknown:
        print(f"claude bridge: ignoring unsupported arguments {unknown}", file=sys.stderr)
    config_path = _env(CONFIG_ENV)
    if not config_path:
        print(f"claude bridge: {CONFIG_ENV} is not set", file=sys.stderr)
        return 1
    proposer = json.loads(Path(config_path).read_text(encoding="utf-8"))
    resumed = args.resume or args.r
    session_id = args.session_id or resumed or uuid.uuid4().hex
    model = args.model or str(proposer.get("model") or _env("ASCENT_MODEL"))
    workspace = Path.cwd()
    session_dir = Path.home() / SESSIONS_DIR / session_id
    session_dir.mkdir(parents=True, exist_ok=True)
    prompt = " ".join(args.prompt).strip() or sys.stdin.read()
    opening = session_dir / "opening-prompt.md"
    # The file's own command has no resume protocol, so its session restates the task, as a fallback harness does.
    if resumed and opening.exists() and proposer.get("resume_command") and not proposer.get("file_command"):
        # The harness continues its own session from the session folder, so it gets only the follow-up.
        proposer = {**proposer, "run_command": proposer["resume_command"]}
    elif resumed and opening.exists():
        # This harness cannot resume a session by id: the workspace carries
        # the progress, so the follow-up restates the task before the nudge.
        prompt = opening.read_text(encoding="utf-8") + _RESUME_NOTE + prompt
    elif not opening.exists():
        opening.write_text(prompt, encoding="utf-8")
    max_cost = float(args.max_budget_usd) if args.max_budget_usd else None
    outcome = run_session(
        proposer,
        workspace=workspace,
        prompt=prompt,
        model=model,
        session_dir=session_dir,
        max_cost_usd=max_cost,
        timeout_seconds=proposer.get("timeout_seconds"),
    )
    record_transcript(session_id, workspace, model, outcome.usage)
    document = result_document(session_id, model, outcome)
    if document["is_error"]:
        # The result text can be the agent's last chat message, which hides why the harness failed.
        print(failure_detail(outcome) or f"harness exited {outcome.returncode}", file=sys.stderr, flush=True)
    print(json.dumps(document))
    sys.stdout.flush()
    if outcome.tool_cap_reached and not outcome.timed_out:
        return 0
    return outcome.returncode if outcome.returncode is not None else 1


if __name__ == "__main__":
    sys.exit(main(sys.argv[1:]))
