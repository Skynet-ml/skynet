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
import hashlib
import json
import os
import re
import shlex
import shutil
import signal
import subprocess
import sys
import threading
import time
import uuid
from collections.abc import Iterator
from dataclasses import dataclass, field
from pathlib import Path, PurePosixPath
from typing import Any

Usage = dict[str, int]

CONFIG_ENV = "APP_PROPOSER_CONFIG"
KEY_ENV = "APP_API_KEY"
# The serialized launch travels through the sandbox input file, so the gateway
# key is replaced by this token and only restored from the process environment.
KEY_TOKEN = "__APP_API_KEY__"
# This file runs alone in the sandbox, without ``core.config``; saved custom
# harnesses and older callers still use these pre-rename spellings.
_LEGACY_PREFIX = "SKYNET_"
_LEGACY_KEY_TOKEN = "__SKYNET_API_KEY__"
SHIM_DIR = ".local/app-bin"
SESSIONS_DIR = ".app-bridge"
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
    """Read an ``APP_*`` variable, falling back to its pre-rename ``SKYNET_*`` name.

    Args:
        name: Variable name starting with ``APP_``.
        default: Value returned when neither name is set.

    Returns:
        The first non-empty value found, else ``default``.
    """
    return os.environ.get(name) or os.environ.get(_LEGACY_PREFIX + name.removeprefix("APP_")) or default


def _with_key(text: str, api_key: str) -> str:
    """Restore the gateway key in place of either key token.

    Args:
        text: A launch command, file or environment value.
        api_key: The route key.

    Returns:
        ``text`` with every key token replaced.
    """
    return text.replace(KEY_TOKEN, api_key).replace(_LEGACY_KEY_TOKEN, api_key)


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
_GUARD_COMMAND_KEYS = frozenset({"command", "cmd", "script"})
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
# cost. Linux also refuses a path behind more than 40 links (ELOOP).
_GUARD_MAX_LINKS = 2_000
_GUARD_MAX_LINK_DEPTH = 40
_GUARD_DEADLINE_SECONDS = 5.0
# Options of these commands that name the folder the command works in.
_GUARD_DIR_OPTIONS = frozenset({"-C", "--chdir", "--directory", "--cwd"})
# One check function shared by the Pi extension and the OpenCode plugin. It
# asks this module for the verdict, so the matching lives in one place.
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


def hidden_kind(path: str) -> str:
    """Name the kind of a hidden path for the run log, without its name.

    Args:
        path: Repository-relative hidden path.

    Returns:
        ``"config"`` for an ``optimize.toml`` file, else ``"scoring"``.
    """
    return "config" if PurePosixPath(path).name.casefold() == "optimize.toml" else "scoring"


def _path_hash(path: str) -> str:
    """Hash one normalized repository-relative path.

    Args:
        path: Relative path with ``/`` separators.

    Returns:
        The SHA-256 hex digest.
    """
    # A tool call can carry a lone surrogate, which strict UTF-8 cannot encode.
    return hashlib.sha256(path.encode("utf-8", errors="surrogatepass")).hexdigest()


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
            ``OPTIMIZE.TOML`` names the same file as ``optimize.toml``.
        home: The agent's home folder, for ``~`` and ``$HOME``. The default
            is this process's home folder.

    Returns:
        The policy: workspace, home folder, path hashes mapped to their
        kind, case folding, and log file.
    """
    hashes = {}
    for path in hidden_paths:
        normalized = os.path.normpath(str(path).strip().strip("/"))
        if normalized in ("", ".") or normalized.startswith(".."):
            continue
        key = normalized.replace(os.sep, "/")
        hashes[_path_hash(key.casefold() if fold_case else key)] = hidden_kind(normalized)
    return {
        "workspace": str(workspace),
        "home": str(home if home is not None else Path.home()),
        "hashes": hashes,
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
    refuses when the call needs too many links.
    """

    def __init__(self) -> None:
        """Start with no links read."""
        self._paths: dict[str, str] = {}
        self._links_left = _GUARD_MAX_LINKS

    def resolve(self, path: str) -> str:
        """Return the real form of an absolute path.

        Args:
            path: An absolute, normalized path.

        Returns:
            The path with every symbolic link resolved. A part that does not
            exist stays as it is, as in ``os.path.realpath``.

        Raises:
            ValueError: When the path needs too many links, or a chain is
                longer than the system allows.
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
        """
        resolved = "/" if path.startswith("/") else start
        for part in path.split("/"):
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
            ValueError: When the call needs too many links, or a chain is
                longer than the system allows.
        """
        known = self._paths.get(path)
        if known is not None:
            return known
        try:
            target = str(Path(path).readlink())
        except OSError:
            self._paths[path] = path
            return path
        self._links_left -= 1
        if self._links_left < 0 or depth >= _GUARD_MAX_LINK_DEPTH:
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
                if relative == "." or relative.startswith(".."):
                    continue
                parts = PurePosixPath(Path(relative).as_posix()).parts
                for end in range(1, len(parts) + 1):
                    kind = hashes.get(_path_hash("/".join(parts[:end])))
                    if kind:
                        return kind
    return None


def _shell_words(command: str, bases: list[str], depth: int = 0) -> Iterator[str]:
    """Yield every word, and every path-like run inside a word, of a shell command.

    Args:
        command: Shell command text.
        bases: Receives the directory of each ``cd`` or ``pushd``, and of each
            folder option such as ``env -C`` or ``git -C``, in the command.
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
        elif word.startswith("-C") and not word.startswith("--") and not word[2:].isdigit():
            bases.append(word[2:])
        # ``cd -P dir`` and ``cd -- dir`` work in ``dir``; a lone ``-`` is the old folder.
        if after_cd and not is_operator and (word == "-" or not word.startswith("-")):
            bases.append(word)
            after_cd = False
        elif is_operator:
            after_cd = False
        elif word in _GUARD_CD_WORDS:
            after_cd = True
        yield word
        yield from _GUARD_WORD_RE.findall(word)
        if depth < _GUARD_SHELL_DEPTH and any(character.isspace() for character in word):
            yield from _shell_words(word, bases, depth + 1)


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
            yield from _shell_words(value, bases)
        elif name in _GUARD_BASE_KEYS:
            bases.append(value)
            yield value
        elif "path" in name or "file" in name or name in _GUARD_PATH_KEYS:
            # A grep pattern is a regex over file contents, not a path.
            if is_grep_pattern:
                continue
            yield value


def guard_check(policy: dict[str, Any], tool: str, tool_input: Any) -> str | None:
    """Tell whether one tool call names a hidden path.

    Args:
        policy: Policy from :func:`guard_policy`.
        tool: The harness's tool name.
        tool_input: The tool's arguments.

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
    named: list[str] = []
    texts: list[str] = []
    home = str(policy.get("home") or "")
    candidates = list(_guard_candidates(tool, tool_input, roots[0], home, named, texts))
    fold = bool(policy.get("fold_case"))
    for text in texts:
        kind = _dynamic_match(text, hashes, fold)
        if kind:
            return kind
    if len(named) > _GUARD_MAX_BASES:
        raise ValueError("too many directory changes to check")
    # A later cd is relative to an earlier one, so each one extends every known directory.
    bases = list(roots)
    for directory in named:
        bases += [os.path.normpath(Path(base, directory)) for base in bases]
        bases = list(dict.fromkeys(bases))
    candidates = list(dict.fromkeys(candidates))
    if len(bases) * sum(1 + candidate.count("/") for candidate in candidates) > _GUARD_MAX_CHECKS:
        raise ValueError("tool call too large to check")
    links = _LinkResolver()
    # A backstop for a cost the caps above miss, such as a slow file system.
    deadline = time.monotonic() + _GUARD_DEADLINE_SECONDS
    for candidate in candidates:
        if time.monotonic() > deadline:
            raise ValueError("tool call took too long to check")
        kind = _hidden_match(candidate, roots, bases, hashes, fold, links)
        if kind:
            return kind
    return None


def guard_main(argv: list[str]) -> int:
    """Answer one tool-call check from a harness hook.

    ``hook`` reads a Codex ``PreToolUse`` event and prints its deny answer.
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
        else:
            tool, tool_input = str(request.get("tool") or ""), request.get("input")
        kind = guard_check(policy, tool, tool_input)
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
    session_dir: Path, workspace: Path, hidden_paths: list[str], home: Path | None = None
) -> dict[str, str]:
    """Write the refusal hooks of every harness for one session.

    Args:
        session_dir: Private directory of the session.
        workspace: Directory the agent works in.
        hidden_paths: Repository-relative hidden files and directories.
        home: The agent's home folder; the default is this process's home folder.

    Returns:
        The environment that points each harness at its hook.
    """
    guard_dir = session_dir / GUARD_DIR
    (guard_dir / "plugin").mkdir(parents=True, exist_ok=True)
    log = guard_dir / "refusals.jsonl"
    log.write_text("", encoding="utf-8")
    policy_file = guard_dir / "policy.json"
    digest = write_guard_policy(policy_file, guard_policy(workspace, hidden_paths, log, home=home))
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
    codex_file = guard_dir / "codex-hooks.json"
    codex_file.write_text(json.dumps(codex_hooks(policy_file, digest)), encoding="utf-8")
    return {
        "APP_GUARD_PI": str(pi_extension),
        "APP_GUARD_CODEX": str(codex_file),
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
                _stop_group(self.process, signal.SIGTERM)

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
            _stop_group(self.process, signal.SIGTERM)


def _stop_group(process: subprocess.Popen[str], signum: int) -> None:
    """Signal the harness shell and every child it spawned.

    Args:
        process: The shell running the harness command, started in its own session.
        signum: Signal to deliver to the whole process group.
    """
    # Signalling only the shell would leave a child holding the stdout pipe open.
    with contextlib.suppress(ProcessLookupError):
        os.killpg(process.pid, signum)


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
            tool calls than this; its workspace edits so far stand), and
            ``hidden_paths`` (repository-relative scoring-side paths every
            tool call that names one is refused).
        workspace: Directory the agent works in (the upstream work dir).
        prompt: Task prompt for this session.
        model: Model identifier the harness should route to.
        session_dir: Private directory for prompt, transcript and logs.
        max_cost_usd: Stop the harness once its priced usage exceeds this.
        timeout_seconds: Kill the harness after this many seconds.
        extra_env: Environment overrides layered on top of the launch env.
        api_key: Route key substituted for the launch's key token; defaults
            to the proposer key in ``APP_API_KEY``.

    Returns:
        The parsed answer, raw transcript, usage and exit status.
    """
    workspace.mkdir(parents=True, exist_ok=True)
    session_dir.mkdir(parents=True, exist_ok=True)
    if api_key is None:
        api_key = _env(KEY_ENV)
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
    env = {
        **os.environ,
        **{name: _with_key(value, api_key) for name, value in (proposer.get("env") or {}).items()},
        **(extra_env or {}),
    }
    hidden_paths = list(proposer.get("hidden_paths") or [])
    if hidden_paths:
        home = Path(env["HOME"]) if env.get("HOME") else None
        env.update(install_guard(session_dir, workspace, hidden_paths, home))
    for name, value in (
        ("MODEL", model),
        ("PROMPT_FILE", str(prompt_file)),
        ("ANSWER_FILE", str(session_dir / f"answer-{attempt}.txt")),
    ):
        env[f"APP_{name}"] = env[_LEGACY_PREFIX + name] = value
    shell = shutil.which("bash") or "/bin/sh"
    started = time.monotonic()
    stderr_file = session_dir / f"run-{attempt}.stderr"
    with stderr_file.open("w", encoding="utf-8") as stderr:
        process = subprocess.Popen(
            [shell, "-c", _with_key(proposer["run_command"], api_key)],
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
        )
        reader = threading.Thread(target=monitor.pump, daemon=True)
        reader.start()
        timed_out = False
        try:
            process.wait(timeout=timeout_seconds)
        except subprocess.TimeoutExpired:
            timed_out = True
            _stop_group(process, signal.SIGKILL)
            process.wait()
        # A child forked while the first signal landed can outlive the shell and
        # keep the pipe open, so keep sweeping the group until the reader sees EOF.
        deadline = time.monotonic() + 5.0
        while (timed_out or monitor.stopped) and reader.is_alive() and time.monotonic() < deadline:
            _stop_group(process, signal.SIGKILL)
            reader.join(timeout=0.2)
        reader.join(timeout=30)
    stdout = "".join(monitor.chunks)
    (session_dir / f"run-{attempt}.stdout").write_text(stdout, encoding="utf-8")
    output_format = proposer.get("output_format", "plain")
    text, usage = parse_output(output_format, stdout)
    refusals = read_refusals(session_dir) if hidden_paths else []
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


def main(argv: list[str]) -> int:
    """Answer one ``claude --print`` invocation with the configured harness.

    Args:
        argv: Command-line arguments after the program name.

    Returns:
        The process exit code: the harness's own code, or 1 for a bridge failure.
    """
    if argv[:1] == ["guard"]:
        return guard_main(argv[1:])
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
    model = args.model or str(proposer.get("model") or _env("APP_MODEL"))
    workspace = Path.cwd()
    session_dir = Path.home() / SESSIONS_DIR / session_id
    session_dir.mkdir(parents=True, exist_ok=True)
    prompt = " ".join(args.prompt).strip() or sys.stdin.read()
    opening = session_dir / "opening-prompt.md"
    if resumed and opening.exists():
        # No other harness resumes a Claude session by id: the workspace carries
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
