"""Read-only view of a GitHub repository for the setup agent's Metric screen.

The agent sees a repository job's structure as a compact,
hierarchical tree (folders collapse to file counts past a depth or line
budget) and opens what it needs through two ReAct tools: one expands a
folder, the other reads a file. Every call goes to GitHub with the caller's
own token, or with no token for a public repository when the caller has no
link, so the agent can only see what the caller can.
"""

from __future__ import annotations

import logging
from collections.abc import Callable
from typing import Any

from ...connectors import github, repo_hosts

logger = logging.getLogger(__name__)

TREE_LINE_BUDGET = 160
TREE_DEPTH = 3
FILES_PER_FOLDER = 12
FOLDER_DEPTH = 2
FILE_BYTES = 40_000
FILE_LINES = 400
READS_PER_TURN = 10
KEY_FILE_BYTES = 6_000

# A read's result: the text, ``{"error": ...}`` when the read failed, or
# ``{"note": ...}`` for an empty folder. The chat card reads the key, never the wording.
RepoRead = str | dict[str, str]

# What an opening look at a repository reads: what the project says it is,
# and what it is built with. Root level only; the first match of each wins.
README_NAMES = ("readme.md", "readme.rst", "readme.txt", "readme")
MANIFEST_NAMES = (
    "package.json",
    "pyproject.toml",
    "setup.py",
    "requirements.txt",
    "cargo.toml",
    "go.mod",
    "pom.xml",
    "build.gradle",
    "build.gradle.kts",
    "gemfile",
    "composer.json",
    "mix.exs",
)

# Generated or vendored folders say nothing about the project and would eat
# the tree's line budget; they stay as one collapsed line.
NOISE_DIRS = frozenset(
    {
        ".git",
        "node_modules",
        "__pycache__",
        ".next",
        "dist",
        "build",
        "out",
        ".venv",
        "venv",
        "vendor",
        ".turbo",
        ".cache",
        "coverage",
        "target",
    }
)


def _build_nodes(entries: list[dict[str, Any]]) -> dict[str, Any]:
    """Nest flat tree entries into ``{name: subtree}`` dicts; files map to ``None``.

    Args:
        entries: ``[{"path", "type": "file" | "dir"}]`` from GitHub.

    Returns:
        The root folder as a nested dict.
    """
    root: dict[str, Any] = {}
    for entry in entries:
        parts = [p for p in str(entry.get("path", "")).split("/") if p]
        if not parts:
            continue
        node = root
        for part in parts[:-1]:
            child = node.get(part)
            if not isinstance(child, dict):
                child = {}
                node[part] = child
            node = child
        leaf = parts[-1]
        if entry.get("type") == "dir":
            if not isinstance(node.get(leaf), dict):
                node[leaf] = {}
        else:
            node.setdefault(leaf, None)
    return root


def _file_count(node: dict[str, Any]) -> int:
    """Count the files under a folder, recursively.

    Args:
        node: A nested folder dict.

    Returns:
        The number of files.
    """
    return sum(1 if child is None else _file_count(child) for child in node.values())


def _render(node: dict[str, Any], *, max_depth: int, line_budget: int, marks: set[str], base: str = "") -> list[str]:
    """Render a folder as an indented tree, collapsing what does not fit.

    Args:
        node: The folder to render.
        max_depth: Folders deeper than this collapse to ``name/ (N files)``.
        line_budget: Stop adding lines past this many.
        marks: Paths to flag as editable by the optimizer.
        base: The folder's own path, for marking.

    Returns:
        The tree lines.
    """
    lines: list[str] = []

    def walk(folder: dict[str, Any], prefix: str, depth: int, path: str) -> None:
        """Append one folder's children to ``lines``."""
        folders = sorted(k for k, v in folder.items() if isinstance(v, dict))
        files = sorted(k for k, v in folder.items() if v is None)
        for name in folders:
            if len(lines) >= line_budget:
                return
            child_path = f"{path}/{name}" if path else name
            child = folder[name]
            mark = "  [editable]" if child_path in marks else ""
            if name in NOISE_DIRS or depth >= max_depth:
                lines.append(f"{prefix}{name}/ ({_file_count(child)} files){mark}")
            else:
                lines.append(f"{prefix}{name}/{mark}")
                walk(child, prefix + "  ", depth + 1, child_path)
        for index, name in enumerate(files):
            if len(lines) >= line_budget:
                return
            if index >= FILES_PER_FOLDER:
                lines.append(f"{prefix}... +{len(files) - index} more files")
                return
            child_path = f"{path}/{name}" if path else name
            mark = "  [editable]" if child_path in marks else ""
            lines.append(f"{prefix}{name}{mark}")

    walk(node, "", 0, base)
    if len(lines) >= line_budget:
        lines.append("... (tree cut here; call list_repo_folder to see more)")
    return lines


def compact_tree(entries: list[dict[str, Any]], editable_paths: list[str]) -> str:
    """Render a repository's tree for the agent's prompt.

    Args:
        entries: ``[{"path", "type"}]`` from GitHub.
        editable_paths: Paths the optimizer may change, flagged in the tree.

    Returns:
        The indented tree, one entry per line.
    """
    marks = {p.strip("/") for p in editable_paths if p.strip("/")}
    return "\n".join(_render(_build_nodes(entries), max_depth=TREE_DEPTH, line_budget=TREE_LINE_BUDGET, marks=marks))


class RepoBrowser:
    """The repository a black-box job optimizes, browsable by the agent's tools.

    Built once per request from the caller's token and the host's tree. The
    caller's tool wrapper emits the chat events for each read.
    """

    def __init__(
        self,
        *,
        token: str | None,
        repository: str,
        branch: str,
        editable_paths: list[str],
        entries: list[dict[str, Any]],
        truncated: bool,
        provider: str = github.PROVIDER,
    ) -> None:
        """Hold the repository, its tree and the caller's token.

        Args:
            token: The caller's token for the repository host; ``None`` reads a
                public GitHub repository anonymously.
            repository: The repository path, ``owner/name`` or a GitLab group path.
            branch: Branch name; empty for the default branch.
            editable_paths: Paths the optimizer may change.
            entries: The tree from the host.
            truncated: Whether the host cut the tree short.
            provider: The repository host.
        """
        self.repository = repository
        self.provider = provider
        self.branch = branch
        self.editable_paths = editable_paths
        self._token = token
        self._entries = entries
        self._truncated = truncated
        self._nodes = _build_nodes(entries)
        self._files = {str(e.get("path")) for e in entries if e.get("type") == "file"}
        self._reads = 0
        self._key_files: list[tuple[str, str]] = []
        # What the agent opened, in order, so its result can name the files.
        self.files_read: list[str] = []

    def _read_text(self, path: str, limit: int) -> tuple[str, bool]:
        """Read one text file from the repository host.

        Args:
            path: File path inside the repository.
            limit: Most bytes to return.

        Returns:
            The text and whether it was cut at ``limit``.
        """
        if self.provider == github.PROVIDER:
            return github.read_text_file(self._token, self.repository, self.branch, path, limit)
        return repo_hosts.host(self.provider).read_text_file(self._token, self.repository, self.branch, path, limit)

    def summary(self) -> str:
        """Describe the repository and its compact tree for the prompt.

        Returns:
            A header line, the editable paths and the tree.
        """
        # The setup agent opens the repository with no editable paths: the Repository screen sets them.
        editable = (
            f"The optimizer may change: {', '.join(self.editable_paths)}."
            if self.editable_paths
            else "The Repository screen sets the paths the optimizer may change."
        )
        lines = [
            f"Repository {self.repository} on branch {self.branch or 'the default branch'}.",
            editable,
            "Tree ([editable] marks what the optimizer may change):",
            compact_tree(self._entries, self.editable_paths),
        ]
        if self._truncated:
            lines.append("GitHub cut the tree short, so some files are missing above.")
        for path, text in self._key_files:
            lines += ["", f"--- {path} (already read for you) ---", text]
        return "\n".join(lines)

    def _key_file_paths(self) -> list[str]:
        """Pick the root README and build manifest an opening look reads.

        Returns:
            Up to two root paths, README first; the first name match of each wins.
        """
        roots = {path.lower(): path for path in self._files if "/" not in path}
        picks = (
            next((roots[name] for name in names if name in roots), None) for names in (README_NAMES, MANIFEST_NAMES)
        )
        return [path for path in picks if path is not None]

    def preload_key_files(self) -> None:
        """Read the root README and build manifest into the summary, for an opening look.

        A light read by design: two root files at most, each capped at
        ``KEY_FILE_BYTES``, never the rest of the repository. A file that
        cannot be read is skipped.
        """
        for path in self._key_file_paths():
            try:
                text, truncated = self._read_text(path, KEY_FILE_BYTES)
            except Exception as exc:  # an unreadable file leaves the opening look without it
                logger.info("Key file %s unreadable in %s: %s", path, self.repository, exc)
                continue
            self._key_files.append((path, text + ("\n... (file cut here)" if truncated else "")))

    def _tool(self, run: Callable[[], RepoRead]) -> RepoRead:
        """Run one read under the per-turn read limit.

        Args:
            run: Produces the observation.

        Returns:
            The observation, or ``{"error": ...}`` for the read limit or a failed read.
        """
        try:
            if self._reads >= READS_PER_TURN:
                return {"error": "Read limit for this turn reached. Work with what you have read, or ask the user."}
            self._reads += 1
            return run()
        except Exception as exc:  # a failed read is an observation, not a failed turn
            logger.info("Repository read failed for %s: %s", self.repository, exc)
            return {"error": f"Could not read it: {exc}"}

    def file_text(self, path: str, limit: int) -> RepoRead:
        """Read one repository file whole, as the base text of an edit.

        The read counts toward the per-turn read limit, like
        :meth:`read_repo_file`.

        Args:
            path: File path inside the repository, exactly as the tree shows it.
            limit: Most bytes the file may have.

        Returns:
            The raw text, or ``{"error": ...}`` when the file does not exist, is
            larger than ``limit``, the read fails, or the read limit is reached.
        """

        def run() -> RepoRead:
            """Fetch the whole file."""
            if path not in self._files:
                return {"error": f"No file named {path!r} in the repository. Check the tree for the exact path."}
            text, truncated = self._read_text(path, limit)
            if truncated:
                return {"error": f"{path} is larger than {limit} bytes, so it cannot be edited here."}
            if path not in self.files_read:
                self.files_read.append(path)
            return text

        return self._tool(run)

    def overview(self) -> RepoRead:
        """Show the repository's tree with its README and manifest, as one read.

        Returns:
            :meth:`summary`, or ``{"error": ...}`` for the read limit.
        """
        return self._tool(self.summary)

    def list_repo_folder(self, path: str) -> RepoRead:
        """Show one folder of the repository two levels deep.

        Call it when the tree in ``repo`` collapsed a folder you need to see
        into a file count. Use ``""`` for the repository root.

        Args:
            path: Folder path inside the repository.

        Returns:
            The folder's tree, ``{"note": ...}`` when it is empty, or
            ``{"error": ...}`` when it does not exist.
        """
        clean = path.strip().strip("/")

        def run() -> RepoRead:
            """Render the folder."""
            node: Any = self._nodes
            for part in [p for p in clean.split("/") if p]:
                node = node.get(part) if isinstance(node, dict) else None
            if not isinstance(node, dict):
                return {"error": f"No folder named {clean!r} in the repository."}
            marks = {p.strip("/") for p in self.editable_paths}
            lines = _render(node, max_depth=FOLDER_DEPTH, line_budget=TREE_LINE_BUDGET, marks=marks, base=clean)
            return "\n".join(lines) if lines else {"note": "The folder is empty."}

        return self._tool(run)

    def read_repo_file(self, path: str) -> RepoRead:
        """Read one file of the repository.

        Open the files that tell you what the project does and how it is
        judged: the README, the package or build manifest, the entry point,
        existing tests, and the files the optimizer may change. Long files
        come back cut.

        Args:
            path: File path inside the repository, exactly as the tree shows it.

        Returns:
            The file's text with line numbers, or ``{"error": ...}`` when it
            does not exist.
        """
        clean = path.strip().strip("/")

        def run() -> RepoRead:
            """Fetch the file."""
            if clean not in self._files:
                return {"error": f"No file named {clean!r} in the repository. Check the tree for the exact path."}
            text, truncated = self._read_text(clean, FILE_BYTES)
            lines = text.splitlines()
            cut = truncated or len(lines) > FILE_LINES
            body = "\n".join(f"{i + 1:>4} {line}" for i, line in enumerate(lines[:FILE_LINES]))
            if clean not in self.files_read:
                self.files_read.append(clean)
            return body + ("\n... (file cut here)" if cut else "")

        return self._tool(run)
