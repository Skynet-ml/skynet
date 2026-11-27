"""Read-only view of a repository or a local folder for the setup agent's Metric screen.

The agent sees a repository job's structure as a compact,
hierarchical tree (folders collapse to file counts past a depth or line
budget) and opens what it needs through two ReAct tools: one expands a
folder, the other reads a file. Every call goes to GitHub with the caller's
own token, or with no token for a public repository when the caller has no
link, so the agent can only see what the caller can.

In desktop local mode the source can be a local folder (SKY-995).
:class:`LocalFolderBrowser` reads it from disk with the same tree and file
output, and never writes to it. :func:`folder_entries`,
:func:`read_folder_text` and :func:`folder_snapshot_id` are the shared
local folder reader.
"""

from __future__ import annotations

import hashlib
import logging
import os
import re
from collections.abc import Callable
from pathlib import Path, PurePosixPath, PureWindowsPath
from typing import Any

from ...api.errors import DomainError
from ...config import settings
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


LOCAL_PROVIDER = "local"


def _source_noun(provider: str | None) -> str:
    """Name the kind of source the agent reads, for its tool texts.

    Args:
        provider: The run's ``repo_provider``.

    Returns:
        ``"folder"`` for a local folder, else ``"repository"``.
    """
    return "folder" if provider == LOCAL_PROVIDER else "repository"


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

    def _header(self) -> str:
        """Name the source on the summary's first line.

        Returns:
            The repository and the branch it is read at.
        """
        return f"Repository {self.repository} on branch {self.branch or 'the default branch'}."

    def _cut_note(self) -> str:
        """Say that the tree is incomplete.

        Returns:
            The note for a tree the host cut short.
        """
        return "GitHub cut the tree short, so some files are missing above."

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
            self._header(),
            editable,
            "Tree ([editable] marks what the optimizer may change):",
            compact_tree(self._entries, self.editable_paths),
        ]
        if self._truncated:
            lines.append(self._cut_note())
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
                return {
                    "error": f"No file named {path!r} in the {_source_noun(self.provider)}. "
                    "Check the tree for the exact path."
                }
            text, truncated = self._read_text(path, limit)
            if truncated:
                return {"error": f"{path} is larger than {limit} bytes, so it cannot be edited here."}
            if path not in self.files_read:
                self.files_read.append(path)
            return text

        return self._tool(run)

    def read_unmetered(self, path: str, limit: int) -> tuple[str, bool] | None:
        """Read one repository file for Skynet's own checks, outside the agent's read limit.

        The read does not count toward the per-turn read limit and is not
        added to ``files_read``: it is not the agent's read.

        Args:
            path: File path inside the repository, exactly as the tree shows it.
            limit: Most bytes to return.

        Returns:
            The text and whether it was cut at ``limit``, or ``None`` when the
            repository has no such file. A failed host read raises.
        """
        if path not in self._files:
            if not self._truncated:
                return None
            # A cut tree may not list the file, so only the host's 404 proves it is missing.
            try:
                return self._read_text(path, limit)
            except DomainError as error:
                if error.code == "connectors.not_found":
                    return None
                raise
        return self._read_text(path, limit)

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
        # A space can be part of a file name, and git keeps it, so only the slashes go.
        clean = path.strip("/")

        def run() -> RepoRead:
            """Render the folder."""
            node: Any = self._nodes
            for part in [p for p in clean.split("/") if p]:
                node = node.get(part) if isinstance(node, dict) else None
            if not isinstance(node, dict):
                return {"error": f"No folder named {clean!r} in the {_source_noun(self.provider)}."}
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
        # A space can be part of a file name, and git keeps it, so only the slashes go.
        clean = path.strip("/")

        def run() -> RepoRead:
            """Fetch the file."""
            if clean not in self._files:
                return {
                    "error": f"No file named {clean!r} in the {_source_noun(self.provider)}. "
                    "Check the tree for the exact path."
                }
            text, truncated = self._read_text(clean, FILE_BYTES)
            lines = text.splitlines()
            cut = truncated or len(lines) > FILE_LINES
            body = "\n".join(f"{i + 1:>4} {line}" for i, line in enumerate(lines[:FILE_LINES]))
            if clean not in self.files_read:
                self.files_read.append(clean)
            return body + ("\n... (file cut here)" if cut else "")

        return self._tool(run)


# A folder past this many entries shows a cut tree, like a host's truncated listing.
FOLDER_ENTRIES_MAX = 20_000
# Git's own data is never part of the source.
FOLDER_SKIP_DIRS = frozenset({".git"})
GITIGNORE = ".gitignore"
# The provider name in a missing-file error, as the hosts' ``connectors.not_found`` carries one.
_FOLDER_LABEL = "Local folder"

# One ``.gitignore`` line: the compiled pattern, whether it starts with ``!``, whether it ends with ``/``.
_IgnoreRule = tuple[re.Pattern[str], bool, bool]


class LocalFolderError(ValueError):
    """A local folder read that is refused: not in local mode, or a path outside the folder."""


def _is_env_file(name: str) -> bool:
    """Say whether a file or folder name is a ``.env*`` name, which can hold secrets.

    Args:
        name: One path part.

    Returns:
        ``True`` for ``.env``, ``.env.local``, ``.envrc`` and the like.
    """
    return name.startswith(".env")


def _glob_regex(glob: str) -> str:
    """Translate one ``.gitignore`` glob into a regular expression.

    Args:
        glob: The pattern, with no leading ``!`` and no trailing ``/``.

    Returns:
        A regex for :func:`re.fullmatch` against a ``/``-separated path.
    """
    out: list[str] = []
    i = 0
    while i < len(glob):
        if glob.startswith("**/", i):
            out.append("(?:.*/)?")
            i += 3
        elif glob.startswith("/**", i) and i + 3 == len(glob):
            out.append("/.*")
            i += 3
        elif glob[i] == "*":
            out.append("[^/]*")
            i += 1
        elif glob[i] == "?":
            out.append("[^/]")
            i += 1
        elif glob[i] == "[" and (end := glob.find("]", i + 2)) > i:
            body = glob[i + 1 : end]
            if body.startswith("!"):
                body = "^" + body[1:]
            out.append("[" + body.replace("\\", "\\\\") + "]")
            i = end + 1
        elif glob[i] == "\\" and i + 1 < len(glob):
            out.append(re.escape(glob[i + 1]))
            i += 2
        else:
            out.append(re.escape(glob[i]))
            i += 1
    return "".join(out)


def _gitignore_rules(text: str) -> list[_IgnoreRule]:
    """Parse one ``.gitignore`` file.

    Args:
        text: The file text.

    Returns:
        The rules in file order; blank lines and comments give none.
    """
    rules: list[_IgnoreRule] = []
    for raw in text.splitlines():
        line = raw.rstrip()
        if not line or line.startswith("#"):
            continue
        negated = line.startswith("!")
        if negated:
            line = line[1:]
        dir_only = line.endswith("/")
        line = line.rstrip("/")
        if not line:
            continue
        # Git anchors a pattern with a "/" before its end to the .gitignore's folder.
        anchored = "/" in line
        regex = _glob_regex(line.lstrip("/"))
        rules.append((re.compile(regex if anchored else "(?:.*/)?" + regex, re.DOTALL), negated, dir_only))
    return rules


def _ignored(rel: str, is_dir: bool, layers: list[tuple[str, list[_IgnoreRule]]]) -> bool:
    """Say whether the ``.gitignore`` files of a path's folders ignore it; the last match wins.

    Args:
        rel: The path from the folder root.
        is_dir: Whether the path is a folder.
        layers: Each ancestor folder that has a ``.gitignore``, root first, with its rules.

    Returns:
        ``True`` when the path is ignored.
    """
    ignored = False
    for base, rules in layers:
        sub = rel[len(base) + 1 :] if base else rel
        for pattern, negated, dir_only in rules:
            if (is_dir or not dir_only) and pattern.fullmatch(sub):
                ignored = not negated
    return ignored


def _folder_root(root: str | Path) -> Path:
    """Check the chosen folder and resolve it.

    Args:
        root: The folder's absolute path.

    Returns:
        The resolved folder.

    Raises:
        LocalFolderError: Outside desktop local mode, for a relative path, or when no folder is there.
    """
    if not settings.local_mode:
        raise LocalFolderError("A local folder can be read only in the desktop app.")
    path = Path(root)
    if not path.is_absolute():
        raise LocalFolderError("The folder path must be absolute.")
    resolved = path.resolve()
    if not resolved.is_dir():
        raise LocalFolderError(f"No folder at {root}.")
    return resolved


def _folder_file(base: Path, path: str) -> Path:
    """Find one file under the folder, and refuse any path that leaves it or names a ``.env*`` file.

    Args:
        base: The resolved folder from :func:`_folder_root`.
        path: The file path from the folder root.

    Returns:
        The resolved file.

    Raises:
        LocalFolderError: For an absolute path, a ``..`` part, a ``.env*`` file, or a link out of the folder.
        DomainError: ``connectors.not_found`` when no file is there.
    """
    clean = path.replace("\\", "/")
    if not clean or clean.startswith("/") or PureWindowsPath(clean).drive:
        raise LocalFolderError(f"{path!r} is not a path inside the folder.")
    parts = PurePosixPath(clean).parts
    if ".." in parts:
        raise LocalFolderError(f"{path!r} leaves the folder.")
    if any(_is_env_file(part) for part in parts):
        raise LocalFolderError(f"{path!r} is a .env file, which can hold secrets, so it is never read.")
    full = (base / clean).resolve()
    if not full.is_relative_to(base):
        raise LocalFolderError(f"{path!r} links out of the folder.")
    if _is_env_file(full.name):
        raise LocalFolderError(f"{path!r} links to a .env file, which can hold secrets, so it is never read.")
    if not full.is_file():
        raise DomainError("connectors.not_found", status=404, provider=_FOLDER_LABEL)
    return full


def folder_entries(root: str | Path) -> tuple[list[dict[str, Any]], bool]:
    """List a local folder as tree entries, with the shape the hosts' tree listings have.

    Skips ``.git``, every ``.env*`` name, what the folder's ``.gitignore``
    files ignore, and links that leave the folder or point at a folder.

    Args:
        root: The folder's absolute path.

    Returns:
        ``[{"path", "type": "file" | "dir"}]`` in name order, and whether the
        list stopped at :data:`FOLDER_ENTRIES_MAX`.

    Raises:
        LocalFolderError: See :func:`_folder_root`.
    """
    base = _folder_root(root)
    entries: list[dict[str, Any]] = []

    def walk(folder: Path, rel: str, layers: list[tuple[str, list[_IgnoreRule]]]) -> bool:
        """Add one folder's entries; return ``False`` once the cap is reached."""
        ignore_file = folder / GITIGNORE
        if ignore_file.is_file() and not ignore_file.is_symlink():
            layers = [*layers, (rel, _gitignore_rules(ignore_file.read_text("utf-8", errors="replace")))]
        try:
            children = sorted(os.scandir(folder), key=lambda child: child.name)
        except OSError:
            return True
        for child in children:
            child_rel = f"{rel}/{child.name}" if rel else child.name
            if _is_env_file(child.name):
                continue
            if child.is_symlink():
                # A followed folder link could loop, so a link counts only as a file inside the folder.
                try:
                    target = Path(child.path).resolve(strict=True)
                except (OSError, RuntimeError):
                    continue
                if not target.is_relative_to(base) or not target.is_file() or _is_env_file(target.name):
                    continue
                is_dir = False
            elif child.is_dir(follow_symlinks=False):
                is_dir = True
            elif child.is_file(follow_symlinks=False):
                is_dir = False
            else:
                continue
            if (is_dir and child.name in FOLDER_SKIP_DIRS) or _ignored(child_rel, is_dir, layers):
                continue
            if len(entries) >= FOLDER_ENTRIES_MAX:
                return False
            entries.append({"path": child_rel, "type": "dir" if is_dir else "file"})
            if is_dir and not walk(Path(child.path), child_rel, layers):
                return False
        return True

    complete = walk(base, "", [])
    return entries, not complete


def read_folder_text(root: str | Path, path: str, limit: int) -> tuple[str, bool]:
    """Read one text file of a local folder, like a host's ``read_text_file``.

    Args:
        root: The folder's absolute path.
        path: The file path from the folder root.
        limit: Most bytes to return.

    Returns:
        The text (bad UTF-8 replaced) and whether it was cut at ``limit``.

    Raises:
        LocalFolderError: See :func:`_folder_root` and :func:`_folder_file`.
        DomainError: ``connectors.not_found`` when no file is there.
    """
    full = _folder_file(_folder_root(root), path)
    with full.open("rb") as handle:
        content = handle.read(limit + 1)
    return content[:limit].decode("utf-8", errors="replace"), len(content) > limit


def folder_snapshot_id(root: str | Path, entries: list[dict[str, Any]] | None = None) -> str:
    """Hash a local folder's listed files, so draft files can name the folder state they were read at.

    The id has the 40-hex shape of a commit, so it fits ``draft_base``.

    Args:
        root: The folder's absolute path.
        entries: The folder's :func:`folder_entries`; listed again when ``None``.

    Returns:
        The SHA-1 over each file's path and content, in path order.

    Raises:
        LocalFolderError: See :func:`_folder_root`.
    """
    base = _folder_root(root)
    listed = folder_entries(base)[0] if entries is None else entries
    digest = hashlib.sha1(usedforsecurity=False)
    for path in sorted(str(entry["path"]) for entry in listed if entry.get("type") == "file"):
        digest.update(path.encode("utf-8", "surrogateescape") + b"\0")
        try:
            with _folder_file(base, path).open("rb") as handle:
                digest.update(hashlib.file_digest(handle, "sha1").digest())
        except (OSError, LocalFolderError, DomainError):
            # A file that changed or went away since the listing still moves the id.
            digest.update(b"\0unreadable")
    return digest.hexdigest()


class LocalFolderBrowser(RepoBrowser):
    """A local folder, browsable by the agent's tools like a repository; it is never written.

    Only in desktop local mode, and only under the chosen folder. ``branch``
    holds the folder's :func:`folder_snapshot_id`, so draft files record it
    as their base, not a commit.
    """

    def __init__(self, root: str | Path) -> None:
        """List the folder and hash its files.

        Args:
            root: The folder's absolute path.

        Raises:
            LocalFolderError: See :func:`_folder_root`.
        """
        base = _folder_root(root)
        entries, truncated = folder_entries(base)
        super().__init__(
            token=None,
            repository=str(base),
            branch=folder_snapshot_id(base, entries),
            editable_paths=[],
            entries=entries,
            truncated=truncated,
            provider=LOCAL_PROVIDER,
        )
        self.root = base

    def _read_text(self, path: str, limit: int) -> tuple[str, bool]:
        """Read one file of the folder from disk.

        Args:
            path: File path from the folder root.
            limit: Most bytes to return.

        Returns:
            The text and whether it was cut at ``limit``.
        """
        return read_folder_text(self.root, path, limit)

    def _header(self) -> str:
        """Name the folder on the summary's first line.

        Returns:
            The folder, and that it has no branch, pull request or host link.
        """
        return f"Local folder {self.repository}. It has no branch, no pull request and no host link."

    def _cut_note(self) -> str:
        """Say that the tree is incomplete.

        Returns:
            The note for a folder past :data:`FOLDER_ENTRIES_MAX` entries.
        """
        return f"The folder has more than {FOLDER_ENTRIES_MAX} entries, so some files are missing above."
