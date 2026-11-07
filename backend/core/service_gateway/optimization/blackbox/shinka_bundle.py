"""Evolve a repository with ShinkaEvolve as a bundle of the files a version changes.

Upstream evolves one program text. For a repository target that text is a
bundle: the full contents of every file the version changes relative to the
run's starting commit, each after a line naming its repository path. The
model never edits the bundle itself; it sees the repository's file list, the
files the current version changes and the files the scorer's feedback
mentions, may ask to open more files, and answers with one ``FILE`` block per
file (SEARCH/REPLACE edits or full contents). Novelty is measured on the
changes alone, and scoring materializes a bundle into the checkout and hands
the parent the same ``git diff`` every other repository engine produces.

Stdlib plus ``repo_tree`` and ``harness_bridge``: they ship into the sandbox beside the runner.
"""

from __future__ import annotations

import difflib
import json
import random
import re
import subprocess
import threading
from collections.abc import Callable, Mapping, Sequence
from pathlib import Path
from typing import Any

try:
    from . import harness_bridge, repo_tree
except ImportError:  # In the sandbox the runner loads these as top-level siblings first.
    import harness_bridge
    import repo_tree

BUNDLE_HEAD = "<!-- APP-BUNDLE: the files this version changes, each after its APP-FILE line -->"
_FILE_LINE = re.compile(r"^<!-- APP-FILE (\{.*\}) -->$")
_FILE_BLOCK = re.compile(r'<FILE\s+path\s*=\s*"([^"\n]+)"\s*>\n?(.*?)\n?</FILE>', re.DOTALL)
_OPEN_REQUEST = re.compile(r"<OPEN>\s*(.*?)\s*</OPEN>", re.DOTALL)
# Upstream's SEARCH/REPLACE grammar, so its models' habits carry over.
_SEARCH_REPLACE = re.compile(r"<{7}\s*SEARCH\s*\n(.*?)\n?\s*={7}\s*\n(.*?)\n?\s*>{7}\s*REPLACE", re.DOTALL)
MAX_FILE_BYTES = 64 * 1024
OPEN_ROUNDS = 2
MAX_OPEN_FILES = 5
_MAX_TREE_ENTRIES = 2000
_MAX_PROMPT_FILE_CHARS = 200_000
_MAX_FEEDBACK_FILES = 5
_MAX_INSPIRATION_DIFF_CHARS = 6000
_MAX_INSPIRATIONS = 4

REPO_SYSTEM_MESSAGE = (
    "You are an expert software engineer improving a code repository so that it scores higher on the "
    "user's scorer. Every version is a set of file changes on top of the run's starting commit. You see "
    "the files you may change, the files the current version changes and the files the scorer's "
    "feedback mentions; ask to open any other file before you edit it."
)
_OPEN_FORMAT = """

# Opening files
To read files you have not been shown, answer with only <OPEN>path</OPEN> lines, at most {files} files per answer; their contents come back in the next message. You can ask {rounds} times at most, so open what you need, then edit."""
_DIFF_FORMAT = """

# Answer format
Answer with an edit name, a description, and one FILE block per file you change, holding exact SEARCH/REPLACE blocks:

<NAME>
short_lowercase_name
</NAME>

<DESCRIPTION>
What you change and why it should score higher.
</DESCRIPTION>

<FILE path="relative/path/to/file">
<<<<<<< SEARCH
lines copied exactly from the file's current contents
=======
the lines that replace them
>>>>>>> REPLACE
</FILE>

Every SEARCH text must match the file's current contents exactly, indentation included; a FILE block may hold several SEARCH/REPLACE blocks. To create a new file, write its full contents in its FILE block without SEARCH/REPLACE markers."""
_FULL_FORMAT = """

# Answer format
Answer with an edit name, a description, and one FILE block holding the complete new contents of every file you change:

<NAME>
short_lowercase_name
</NAME>

<DESCRIPTION>
What you change and why it should score higher.
</DESCRIPTION>

<FILE path="relative/path/to/file">
the file's complete new contents
</FILE>

Files you do not write keep their current contents."""
_INTROS = {
    "diff": "Improve the current version with targeted edits.",
    "full": "Improve the current version by rewriting the files you change in full.",
    "cross": (
        "Combine the strengths of the current version and the other version below into one better version. "
        "For every file where the two differ, decide what the merged file should hold and write it in full."
    ),
    "fix": "The current version failed to score. Find the cause in the feedback and fix it by rewriting the files in full.",
    "agent": "Improve the current version by editing its files in your working directory.",
}


def encode_bundle(files: Mapping[str, str]) -> str:
    """Write a version's changed files as one program text.

    Each file follows a line naming its path and line count, so any file
    contents, marker-like lines included, decode back exactly.

    Args:
        files: Repository-relative path to full file contents.

    Returns:
        The bundle text upstream stores as the version's program.
    """
    lines = [BUNDLE_HEAD]
    for path in sorted(files):
        pieces = files[path].split("\n")
        header = json.dumps({"path": path, "lines": len(pieces)}, ensure_ascii=True).replace(">", "\\u003e")
        lines.append(f"<!-- APP-FILE {header} -->")
        lines.extend(pieces)
    return "\n".join(lines) + "\n"


def decode_bundle(text: str) -> dict[str, str]:
    """Read a bundle back into its files.

    Args:
        text: Bundle text written by :func:`encode_bundle`.

    Returns:
        Repository-relative path to full file contents.

    Raises:
        ValueError: When the text is not a well-formed bundle.
    """
    lines = text.split("\n")
    if not lines or lines[0] != BUNDLE_HEAD:
        raise ValueError("The version is not a repository bundle.")
    files: dict[str, str] = {}
    index = 1
    while index < len(lines):
        if not lines[index] and index == len(lines) - 1:
            break
        match = _FILE_LINE.match(lines[index])
        if match is None:
            raise ValueError("A repository bundle line is outside every file.")
        try:
            header = json.loads(match.group(1))
            path, count = str(header["path"]), int(header["lines"])
        except (ValueError, KeyError, TypeError) as exc:
            raise ValueError("A repository bundle file line is not valid.") from exc
        if count < 1 or index + count >= len(lines):
            raise ValueError(f"The bundle entry for '{path}' is cut short.")
        files[path] = "\n".join(lines[index + 1 : index + 1 + count])
        index += 1 + count
    return files


def parse_response(text: str) -> tuple[list[tuple[str, str]], list[str]]:
    """Read the file edits and open requests out of a model answer.

    Args:
        text: The model's answer.

    Returns:
        ``(path, body)`` per FILE block in answer order, and the requested paths.
    """
    edits = [(path.strip().removeprefix("./"), body) for path, body in _FILE_BLOCK.findall(text)]
    opens = [path.strip() for path in _OPEN_REQUEST.findall(text) if path.strip()]
    return edits, list(dict.fromkeys(opens))


def apply_file_edit(body: str, current: str | None) -> tuple[str, int]:
    """Apply one FILE block to a file's current contents.

    Args:
        body: The FILE block's text: SEARCH/REPLACE blocks, or full contents.
        current: The file's current contents, or ``None`` for a new file.

    Returns:
        The new contents and how many edits it took.

    Raises:
        ValueError: When a SEARCH text is not in the file, or a missing file is edited by SEARCH/REPLACE.
    """
    blocks = _SEARCH_REPLACE.findall(body)
    if not blocks:
        return (body if body.endswith("\n") or not body else body + "\n"), 1
    if current is None:
        raise ValueError("it does not exist yet; write its full contents without SEARCH/REPLACE markers")
    updated = current
    for search, replace in blocks:
        if not search.strip():
            updated = updated + ("" if updated.endswith("\n") or not updated else "\n") + replace + "\n"
            continue
        if search in updated:
            updated = updated.replace(search, replace, 1)
            continue
        loose = _loose_find(search, updated)
        if loose is None:
            raise ValueError("a SEARCH text does not match the file's current contents:\n" + search[:600])
        start, end = loose
        updated = updated[:start] + replace + updated[end:]
    return updated, len(blocks)


def _loose_find(search: str, text: str) -> tuple[int, int] | None:
    """Find ``search`` in ``text`` ignoring trailing whitespace on each line.

    Args:
        search: Lines to find.
        text: File contents.

    Returns:
        The matched character span, or ``None``.
    """
    wanted = [line.rstrip() for line in search.split("\n")]
    lines = text.split("\n")
    offsets = [0]
    for line in lines:
        offsets.append(offsets[-1] + len(line) + 1)
    for first in range(len(lines) - len(wanted) + 1):
        if all(lines[first + at].rstrip() == wanted[at] for at in range(len(wanted))):
            last = first + len(wanted) - 1
            return offsets[first], offsets[last] + len(lines[last])
    return None


def mentioned_paths(feedback: str, paths: Sequence[str], limit: int = _MAX_FEEDBACK_FILES) -> list[str]:
    """List the repository files a scorer's feedback names.

    A full path counts wherever it appears; a bare file name counts only as a
    whole word and only when one file carries it.

    Args:
        feedback: Scorer feedback text.
        paths: Files the model may change.
        limit: Most files to return.

    Returns:
        Mentioned paths in the order they first appear in the feedback.
    """
    if not feedback:
        return []
    by_name: dict[str, list[str]] = {}
    for path in paths:
        by_name.setdefault(path.rsplit("/", 1)[-1], []).append(path)
    found: list[tuple[int, str]] = []
    for path in paths:
        at = feedback.find(path)
        if at < 0 and "/" in path and len(by_name[path.rsplit("/", 1)[-1]]) == 1:
            match = re.search(rf"(?<![\w./-]){re.escape(path.rsplit('/', 1)[-1])}(?![\w-])", feedback)
            at = match.start() if match else -1
        if at >= 0:
            found.append((at, path))
    return [path for _, path in sorted(found)][:limit]


def file_diff(path: str, old: str | None, new: str | None) -> str:
    """Render one file's change as a unified diff.

    Args:
        path: Repository-relative path.
        old: Contents before, or ``None`` when the file is new.
        new: Contents after, or ``None`` when the file is gone.

    Returns:
        The diff text; empty when nothing changed.
    """
    return "".join(
        difflib.unified_diff(
            (old or "").splitlines(keepends=True),
            (new or "").splitlines(keepends=True),
            fromfile=f"a/{path}",
            tofile=f"b/{path}",
            n=3,
        )
    )


def choose_patch_type(types: Sequence[str], probs: Sequence[float], has_partner: bool, rng: Any = random) -> str:
    """Pick a mutation kind the way upstream does, never crossover without a partner.

    Args:
        types: Upstream patch types.
        probs: Their probabilities.
        has_partner: Whether an inspiration version exists to cross with.
        rng: Random source.

    Returns:
        ``diff``, ``full`` or ``cross``.
    """
    weights = [float(p) if has_partner or kind != "cross" else 0.0 for kind, p in zip(types, probs, strict=True)]
    if sum(weights) <= 0:
        weights = [0.0 if kind == "cross" else 1.0 for kind in types]
    return str(rng.choices(list(types), weights=weights)[0])


def _fence(path: str, text: str) -> str:
    """Show one file in a prompt.

    Args:
        path: Repository-relative path.
        text: File contents.

    Returns:
        The file under a heading, fenced so its own backticks cannot end the block.
    """
    longest = max((len(run) for run in re.findall(r"`+", text)), default=0)
    fence = "`" * max(3, longest + 1)
    return f"## {path}\n{fence}\n{text}{'' if text.endswith(chr(10)) else chr(10)}{fence}\n"


def _score_text(program: Any) -> str:
    """Describe a version's score for a prompt.

    Args:
        program: Upstream program record.

    Returns:
        The score, or a note that it has none.
    """
    score = getattr(program, "combined_score", None)
    return f"{float(score):.4g}" if isinstance(score, int | float) else "not scored"


class RepoBundle:
    """The run's repository checkout and the rules a bundle version must follow."""

    def __init__(
        self,
        checkout: Path,
        editable_paths: Sequence[str],
        readonly_paths: Sequence[str],
        on_block: Callable[[str], None] | None = None,
        hidden_paths: Sequence[str] = (),
        on_refusal: Callable[[dict[str, str]], None] | None = None,
    ) -> None:
        """Bind to a checkout whose ``HEAD`` is the run's starting commit.

        Args:
            checkout: Folder made by ``repo_tree.unpack_tree``, seed already committed.
            editable_paths: Files and folders a version may change.
            readonly_paths: Submodules and Git LFS files, which stay as fetched.
            on_block: Under a constraints file, receives each path-rule
                refusal's reason, and the model reads only the illegal-action message.
            hidden_paths: Scoring-side files and folders the model may not
                open, list or edit.
            on_refusal: Receives each hidden-path refusal as ``{"tool", "kind"}``.
        """
        self.checkout = checkout
        self.on_block = on_block
        self.hidden_paths = [str(path).strip("/") for path in hidden_paths if str(path).strip("/")]
        self.on_refusal = on_refusal
        self.editable_paths = [str(path) for path in editable_paths]
        self.readonly_paths = [str(path) for path in readonly_paths]
        self.start = self._git("rev-parse", "HEAD").strip()
        self._lock = threading.Lock()
        self._texts: dict[str, str | None] = {}
        self._paths: list[str] | None = None

    @classmethod
    def prepare(
        cls,
        repo: Mapping[str, Any],
        seed_patch: Any,
        destination: Path,
        on_block: Callable[[str], None] | None = None,
        on_refusal: Callable[[dict[str, str]], None] | None = None,
    ) -> RepoBundle:
        """Unpack the shipped tree and commit the seed version on top as the starting commit.

        Args:
            repo: Parent payload's ``repo`` entry: tree chunks, path rules and
                whether a constraints file governs the run.
            seed_patch: Starting version as a patch against the fetched commit, or empty.
            destination: Folder the checkout goes in.
            on_block: Receives each path-rule block's reason when ``repo`` is constrained.
            on_refusal: Receives each refusal of a path in ``repo``'s ``hidden_paths``.

        Returns:
            The bundle rules bound to the new checkout.
        """
        checkout = repo_tree.unpack_tree((Path(chunk) for chunk in repo["chunks"]), destination)
        if isinstance(seed_patch, str) and seed_patch.strip():
            repo_tree.apply_patch(checkout, seed_patch)
            _run_git(checkout, "add", "--all")
            _run_git(checkout, "commit", "--quiet", "--no-verify", "-m", "seed")
        return cls(
            checkout,
            repo["editable_paths"],
            repo.get("readonly_paths") or (),
            on_block=on_block if repo.get("constrained") else None,
            hidden_paths=repo.get("hidden_paths") or (),
            on_refusal=on_refusal,
        )

    def _git(self, *arguments: str) -> str:
        """Run git in the checkout.

        Args:
            *arguments: Arguments after ``git``.

        Returns:
            Standard output.
        """
        return _run_git(self.checkout, *arguments)

    def rule_problem(self, path: str) -> str | None:
        """Explain why a version may not change a path.

        Args:
            path: Repository-relative path.

        Returns:
            The reason, or ``None`` when the path is editable.
        """
        parts = path.split("/")
        if not path or path.startswith("/") or any(part in ("", ".", "..") for part in parts) or ".git" in parts:
            return "it is not a path inside the repository"
        if _covers(self.readonly_paths, path):
            return "submodules and Git LFS files are read-only"
        if not _covers(self.editable_paths, path):
            return "it is outside the editable paths"
        return None

    def hidden_refusal(self, path: str, tool: str) -> str | None:
        """Refuse a hidden path, and report the tool and the path kind.

        Args:
            path: Repository-relative path the model named.
            tool: ``open`` or ``edit``.

        Returns:
            The refusal text, or ``None`` when the path is not hidden.
        """
        normalized = path.strip().strip("/")
        if not _covers_exact(self.hidden_paths, normalized):
            return None
        if self.on_refusal is not None:
            self.on_refusal({"tool": tool, "kind": harness_bridge.hidden_kind(normalized)})
        return harness_bridge.GUARD_REFUSAL

    def refusal(self, path: str, problem: str) -> str:
        """Word a path-rule refusal, and log it as a block under a constraints file.

        Args:
            path: Repository-relative path the version may not change.
            problem: The reason from :meth:`rule_problem`.

        Returns:
            The illegal-action message under a constraints file, else the reason naming the path.
        """
        reason = f"Cannot change '{path}': {problem}."
        if self.on_block is None:
            return reason
        self.on_block(reason)
        return repo_tree.ILLEGAL_ACTION_MESSAGE

    def paths(self) -> list[str]:
        """List the starting commit's text files a version may change.

        Returns:
            Repository-relative paths, sorted.
        """
        if self._paths is None:
            sized = {}
            for entry in self._git("ls-tree", "-r", "-l", "-z", self.start).split("\0"):
                meta, _, path = entry.partition("\t")
                fields = meta.split()
                if len(fields) == 4 and fields[1] == "blob" and fields[0] in ("100644", "100755"):
                    sized[path] = int(fields[3]) if fields[3].isdigit() else MAX_FILE_BYTES + 1
            text = {line.split(":", 1)[1] for line in self._grep_text_files() if ":" in line}
            self._paths = sorted(
                path
                for path, size in sized.items()
                if size <= MAX_FILE_BYTES
                and (size == 0 or path in text)
                and self.rule_problem(path) is None
                and not _covers_exact(self.hidden_paths, path)
            )
        return self._paths

    def _grep_text_files(self) -> list[str]:
        """List the starting commit's non-empty text files as ``<commit>:<path>``.

        Returns:
            One entry per file git does not consider binary.
        """
        result = subprocess.run(
            ["git", "grep", "-z", "-I", "-l", "-e", "", self.start, "--"],
            cwd=self.checkout,
            capture_output=True,
            text=True,
            check=False,
        )
        # git grep exits 1 when nothing matches, which is an empty listing, not a failure.
        return [line for line in result.stdout.split("\0") if line] if result.returncode in (0, 1) else []

    def start_text(self, path: str) -> str | None:
        """Read a file as the starting commit holds it.

        Args:
            path: Repository-relative path.

        Returns:
            Its text, or ``None`` when it is missing, binary, too large or not editable.
        """
        if path not in self._texts:
            text = None
            if path in set(self.paths()):
                blob = subprocess.run(
                    ["git", "cat-file", "blob", f"{self.start}:{path}"],
                    cwd=self.checkout,
                    capture_output=True,
                    check=False,
                )
                if blob.returncode == 0 and b"\0" not in blob.stdout:
                    try:
                        text = blob.stdout.decode("utf-8")
                    except UnicodeDecodeError:
                        text = None
            self._texts[path] = text
        return self._texts[path]

    def exists_at_start(self, path: str) -> bool:
        """Report whether the starting commit has anything at a path.

        Args:
            path: Repository-relative path.

        Returns:
            Whether git knows the path at the starting commit.
        """
        result = subprocess.run(
            ["git", "cat-file", "-e", f"{self.start}:{path}"], cwd=self.checkout, capture_output=True, check=False
        )
        return result.returncode == 0

    def current(self, files: Mapping[str, str], path: str) -> str | None:
        """Read a file as a version holds it.

        Args:
            files: The version's bundle files.
            path: Repository-relative path.

        Returns:
            Its text, or ``None`` when the version has no such text file.
        """
        return files[path] if path in files else self.start_text(path)

    def normalize(self, files: Mapping[str, str]) -> dict[str, str]:
        """Drop files a version holds exactly as the starting commit does.

        Args:
            files: Bundle files.

        Returns:
            Only the files that differ from the starting commit.
        """
        return {path: text for path, text in files.items() if text != self.start_text(path)}

    def apply(self, files: Mapping[str, str], response: str) -> tuple[dict[str, str] | None, int, str | None]:
        """Apply a model answer to a version.

        Args:
            files: The parent version's bundle files.
            response: The model's answer.

        Returns:
            ``(files, edits, None)`` on success; ``(None, 0, message)`` when the
            answer only opens files or cannot be applied, the message going back
            to the model.
        """
        edits, opens = parse_response(response)
        if not edits:
            if opens:
                return None, 0, self.open_files(files, opens)
            return None, 0, 'No FILE blocks found. Answer with <FILE path="..."> blocks or <OPEN>path</OPEN> lines.'
        updated = dict(files)
        applied = 0
        for path, body in edits:
            hidden = self.hidden_refusal(path, "edit")
            if hidden is not None:
                return None, 0, hidden
            problem = self.rule_problem(path)
            if problem is not None:
                return None, 0, self.refusal(path, problem)
            current = self.current(updated, path)
            if current is None and self.exists_at_start(path):
                problem = f"it is binary or larger than {MAX_FILE_BYTES // 1024} KiB, so it cannot be edited"
                return None, 0, f"Cannot change '{path}': {problem}."
            try:
                updated[path], count = apply_file_edit(body, current)
            except ValueError as exc:
                return None, 0, f"Cannot change '{path}': {exc}"
            applied += count
        updated = self.normalize(updated)
        if updated == self.normalize(files):
            return None, 0, "The edit leaves every file as it was."
        return updated, applied, None

    def open_files(self, files: Mapping[str, str], paths: Sequence[str]) -> str:
        """Show the files a model asked to open.

        Args:
            files: The version the model is editing.
            paths: Requested paths.

        Returns:
            A message holding each file's current contents or why it cannot be shown.
        """
        shown = [
            "No edit was made yet. Here are the files you asked to open; answer with your edit next.",
            "",
        ]
        for path in paths[:MAX_OPEN_FILES]:
            hidden = self.hidden_refusal(path, "open")
            if hidden is not None:
                shown.append(f"## {path}\n{hidden}\n")
                continue
            text = self.current(files, path)
            if text is None:
                problem = self.rule_problem(path) or "it is missing, binary or too large to show"
                shown.append(f"## {path}\nNot available: {problem}.\n")
            else:
                shown.append(_fence(path, text))
        if len(paths) > MAX_OPEN_FILES:
            shown.append(f"Only the first {MAX_OPEN_FILES} requested files are shown.")
        return "\n".join(shown)

    def changes_text(self, files: Mapping[str, str]) -> str:
        """Render a version's change against the starting commit as diffs.

        Args:
            files: Bundle files.

        Returns:
            One unified diff per changed file, in path order.
        """
        return "".join(file_diff(path, self.start_text(path), files[path]) for path in sorted(files))

    def materialize(self, files: Mapping[str, str]) -> str:
        """Write a version into the checkout and return it as the run's patch.

        Args:
            files: Bundle files.

        Returns:
            ``git diff`` against the fetched commit, the seed's changes included.

        Raises:
            ValueError: When a bundle path breaks the editing rules.
        """
        for path in files:
            problem = self.rule_problem(path)
            if problem is not None:
                raise ValueError(self.refusal(path, problem))
        with self._lock:
            self._git("reset", "--quiet", "--hard", self.start)
            self._git("clean", "--quiet", "-fdx")
            for path, text in files.items():
                target = self.checkout / path
                target.parent.mkdir(parents=True, exist_ok=True)
                target.write_text(text, encoding="utf-8")
            return repo_tree.version_patch(self.checkout)

    def write_patch(
        self, original: str, response: str, patch_dir: Path
    ) -> tuple[str | None, int, Path | None, str | None, str | None, Path | None]:
        """Stand in for upstream's patch application on a bundle version.

        Args:
            original: The parent's bundle text.
            response: The model's answer.
            patch_dir: Upstream's generation folder.

        Returns:
            Upstream's ``(code, applied, output_path, error, patch_text, patch_path)``.
        """
        try:
            parent = decode_bundle(original)
        except ValueError as exc:
            return None, 0, None, str(exc), None, None
        files, applied, error = self.apply(parent, response)
        if files is None:
            return None, 0, None, error, None, None
        patch_dir.mkdir(parents=True, exist_ok=True)
        code = encode_bundle(files)
        (patch_dir / "original.md").write_text(original, encoding="utf-8")
        output = patch_dir / "main.md"
        output.write_text(code, encoding="utf-8")
        diff = "".join(
            file_diff(path, self.current(parent, path), self.current(files, path))
            for path in sorted(set(parent) | set(files))
        )
        patch_path = patch_dir / "edit.diff"
        patch_path.write_text(diff, encoding="utf-8")
        return code, applied, output, None, diff, patch_path

    def prompt(
        self,
        kind: str,
        parent: Any,
        *,
        system_message: str,
        inspirations: Sequence[Any] = (),
        partner: Any = None,
        meta_recommendations: str | None = None,
        use_text_feedback: bool = True,
    ) -> tuple[str, str]:
        """Build the system and user messages for one mutation of a bundle version.

        Args:
            kind: ``diff``, ``full``, ``cross``, ``fix``, or ``agent`` for a
                coding agent that edits the files itself and so gets no
                answer format.
            parent: Upstream program record being mutated.
            system_message: The run's task system message.
            inspirations: Other scored versions to learn from.
            partner: The version a crossover combines with the parent.
            meta_recommendations: Upstream's meta notes, if any.
            use_text_feedback: Whether to show the scorer's feedback.

        Returns:
            ``(system_message, user_message)``.
        """
        files = _safe_decode(getattr(parent, "code", ""))
        paths = self.paths()
        system = system_message
        if meta_recommendations not in (None, "none") and kind != "cross":
            system += f"\n\n# Potential Recommendations\n{meta_recommendations}"
        if kind != "agent":
            system += _OPEN_FORMAT.format(files=MAX_OPEN_FILES, rounds=OPEN_ROUNDS)
            system += _DIFF_FORMAT if kind == "diff" else _FULL_FORMAT
        budget = [_MAX_PROMPT_FILE_CHARS]
        sections = [_INTROS.get(kind, _INTROS["diff"])]
        listing = paths[:_MAX_TREE_ENTRIES]
        more = f"\n... and {len(paths) - len(listing)} more" if len(paths) > len(listing) else ""
        extra = sorted(path for path in files if path not in set(paths))
        sections.append(
            f"# Repository files\nThe text files you may change ({len(paths)}):\n"
            + "\n".join(listing)
            + more
            + (("\nNew files this version adds:\n" + "\n".join(extra)) if extra else "")
        )
        current = [f"# Current version\nScore: {_score_text(parent)}"]
        if files:
            current.append("The files this version changes from the starting commit, as it holds them now:")
            current.extend(self._show(path, files[path], budget) for path in sorted(files))
        else:
            current.append("It is the starting commit itself; it changes no files yet.")
        sections.append("\n".join(current))
        feedback = str(getattr(parent, "text_feedback", "") or "").strip() if use_text_feedback else ""
        if feedback:
            sections.append(f"# Scorer feedback\n{feedback}")
            named = [path for path in mentioned_paths(feedback, paths) if path not in files]
            if named:
                sections.append(
                    "# Files the feedback mentions\n"
                    + "\n".join(self._show(path, self.start_text(path) or "", budget) for path in named)
                )
        others = [program for program in inspirations if program is not parent and program is not partner]
        if others:
            history = ["# Earlier versions\nOther scored versions, as diffs against the starting commit:"]
            for program in others[:_MAX_INSPIRATIONS]:
                diff = self.changes_text(_safe_decode(getattr(program, "code", "")))
                if len(diff) > _MAX_INSPIRATION_DIFF_CHARS:
                    diff = diff[:_MAX_INSPIRATION_DIFF_CHARS] + "\n[diff truncated]\n"
                history.append(f"## Version scoring {_score_text(program)}\n```diff\n{diff or '(no changes)'}\n```")
            sections.append("\n".join(history))
        if partner is not None:
            other = _safe_decode(getattr(partner, "code", ""))
            differing = sorted(path for path in set(files) | set(other) if files.get(path) != other.get(path))
            cross = [f"# Other version\nScore: {_score_text(partner)}. The files where the two versions differ:"]
            for path in differing:
                cross.append(f"### {path} in the current version")
                cross.append(self._show(path, self.current(files, path) or "(absent)", budget))
                cross.append(f"### {path} in the other version")
                cross.append(self._show(path, self.current(other, path) or "(absent)", budget))
            sections.append("\n".join(cross))
        return system, "\n\n".join(sections)

    def _show(self, path: str, text: str, budget: list[int]) -> str:
        """Show a file in a prompt while the prompt's file budget lasts.

        Args:
            path: Repository-relative path.
            text: Contents to show.
            budget: Characters left for file contents; reduced in place.

        Returns:
            The fenced file, or a note to open it when the budget is spent.
        """
        if len(text) > budget[0]:
            return f"## {path}\n(Not shown to keep the prompt small; open it to read it.)\n"
        budget[0] -= len(text)
        return _fence(path, text)


def _safe_decode(text: Any) -> dict[str, str]:
    """Decode a stored version, treating anything unreadable as no changes.

    Args:
        text: Program text upstream stored.

    Returns:
        The version's bundle files.
    """
    try:
        return decode_bundle(str(text or ""))
    except ValueError:
        return {}


def _covers(roots: Sequence[str], path: str) -> bool:
    """Report whether a path is one of ``roots`` or lies below one.

    Args:
        roots: Repository-relative files or folders; ``.`` is the whole tree.
        path: Repository-relative path.

    Returns:
        Whether a root covers the path.
    """
    return any(root == "." or path == root or path.startswith(root.rstrip("/") + "/") for root in roots)


def _covers_exact(roots: Sequence[str], path: str) -> bool:
    """Report whether a path is one of ``roots`` or lies below one, with no whole-tree root.

    Args:
        roots: Repository-relative hidden files or folders.
        path: Repository-relative path.

    Returns:
        Whether a root covers the path.
    """
    return any(path == root or path.startswith(root + "/") for root in roots)


def _run_git(checkout: Path, *arguments: str) -> str:
    """Run git in a checkout and return its output.

    Args:
        checkout: Checkout root.
        *arguments: Arguments after ``git``.

    Returns:
        Standard output.
    """
    return repo_tree._git(arguments, checkout)
