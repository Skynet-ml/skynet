"""Ship a repository tree into a sandbox and check the versions written against it.

A repository version is a ``git diff --binary`` against the run's starting
tree. Stdlib only: the native runner ships this module into its sandbox so
the agent's workspace and the trusted scorer apply the same rules.
"""

from __future__ import annotations

import base64
import posixpath
import re
import subprocess
import tarfile
from collections.abc import Iterable, Iterator, Sequence
from pathlib import Path
from typing import Any

# Payload key naming the packed tree. The parent sets it to a file on its own
# disk; the sandbox supervisor uploads that file and rewrites the key into
# guest chunk paths. Secrets never ride in a payload, only their names do.
REPO_SNAPSHOT_KEY = "_repo_snapshot"
# Uploads are text and capped at 16 MiB per request: 9 MiB of bytes is 12 MiB of base64.
ARCHIVE_CHUNK_BYTES = 9 * 1024 * 1024
_GIT_IDENTITY = ["-c", "user.name=app", "-c", "user.email=app@localhost", "-c", "commit.gpgsign=false"]

# A run with a constraints file answers every refused action with this fixed
# text, both in the sandbox and in the parent, and logs the reason as a
# ``rule.block`` run event instead. ``rule_judge`` and its tests use these.
ILLEGAL_ACTION_MESSAGE = (
    "Illegal action. Read the repository rules in your brief. You cannot do this action. Do not repeat it,"
    " because it wastes time."
)
RULE_BLOCK_EVENT = "rule.block"
PATH_RULE = "optimize.toml [paths]"


def rule_block_record(reason: str) -> dict[str, Any]:
    """Build the structured run-log record a sandboxed engine sends for one path-rule block.

    Args:
        reason: Why the path rules refused the action.

    Returns:
        The record the parent forwards to the run log as a ``rule.block`` event.
    """
    return {
        "level": "WARNING",
        "logger": "app.engine",
        "message": f"Illegal action blocked: {PATH_RULE}. {reason}",
        "source": "engine",
        "event": RULE_BLOCK_EVENT,
        "fields": {"rule": PATH_RULE, "reason": reason},
    }


# The shipped tree's commit. Versions diff against this ref rather than HEAD,
# so an agent that commits in its checkout cannot hide those edits.
BASE_REF = "refs/app/base"
# The exploit suite ships in the improvement PR, so no version may rewrite it
# (SKY-356). Same value as ``core.constants.HARDENING_EXPLOITS_DIR``; this
# module is stdlib-only, so it cannot import that.
EXPLOITS_DIR = ".optimize/exploits"
# A version travels as one sandbox file, and uploads cap each file at 16 MiB.
MAX_PATCH_BYTES = 8 * 1024 * 1024

_GITLINK_MODE = "160000"
_MODE_LINE = re.compile(r"^(?:new file mode|deleted file mode|old mode|new mode) (\d{6})$")
_INDEX_MODE = re.compile(r"^index [0-9a-f]+\.\.[0-9a-f]+ (\d{6})$")
_PATH_PREFIXES = ("rename from ", "rename to ", "copy from ", "copy to ")
_HUNK = re.compile(r"^@@ -\d+(?:,(\d+))? \+\d+(?:,(\d+))? @@")
_FILE_LINE = re.compile(r'^(?:--- (?:a/|"a/|/dev/null)|\+\+\+ (?:b/|"b/|/dev/null))')


def _unquote(token: str) -> str:
    """Decode a C-quoted git path, octal escapes included.

    Args:
        token: Path as git printed it, with or without surrounding quotes.

    Returns:
        The path's text.
    """
    if not (len(token) >= 2 and token.startswith('"') and token.endswith('"')):
        return token
    raw = token[1:-1].encode("latin-1", "backslashreplace")
    return raw.decode("unicode_escape").encode("latin-1").decode("utf-8", "replace")


def _split_quoted(rest: str) -> list[str]:
    """Split the two quoted or bare tokens of a ``diff --git`` header.

    Args:
        rest: The header after ``diff --git ``.

    Returns:
        The tokens, quotes still attached.
    """
    tokens: list[str] = []
    index = 0
    while index < len(rest):
        if rest[index] == " ":
            index += 1
            continue
        if rest[index] == '"':
            end = index + 1
            while end < len(rest) and rest[end] != '"':
                end += 2 if rest[end] == "\\" else 1
            tokens.append(rest[index : end + 1])
            index = end + 1
        else:
            end = rest.find(" ", index)
            end = len(rest) if end == -1 else end
            tokens.append(rest[index:end])
            index = end
    return tokens


def _header_paths(rest: str) -> list[str]:
    """Read the paths of one ``diff --git a/X b/Y`` header.

    An unquoted header is ambiguous when names hold spaces, but git prints the
    same path twice unless the file was renamed, and a rename also writes
    ``rename from``/``rename to`` lines, so the equal halves split is enough.

    Args:
        rest: The header after ``diff --git ``.

    Returns:
        The paths without their ``a/``/``b/`` prefixes; empty when unreadable.
    """
    if rest.startswith('"') or rest.endswith('"'):
        tokens = [_unquote(token) for token in _split_quoted(rest)]
        return [token[2:] for token in tokens if token[:2] in ("a/", "b/")]
    half = (len(rest) - len("a/ b/")) // 2
    first, separator, second = rest[2 : 2 + half], rest[2 + half : 5 + half], rest[5 + half :]
    if rest.startswith("a/") and separator == " b/" and first == second:
        return [first]
    return []


def patch_paths(patch: str) -> list[str]:
    """List every repository path a patch creates, changes, renames, or deletes.

    Args:
        patch: ``git diff`` output.

    Returns:
        The distinct paths in first-seen order.
    """
    found: list[str] = []
    # Hunk bodies can hold lines that look like headers ("--- a/x" is a
    # removed "-- a/x"), so only the lines before a file's first hunk count.
    in_header = False
    for line in patch.split("\n"):
        if line.startswith("diff --git "):
            in_header = True
            found.extend(_header_paths(line[len("diff --git ") :]))
        elif not in_header:
            continue
        elif line.startswith(("@@", "GIT binary patch")):
            in_header = False
        elif line.startswith(("--- a/", "+++ b/")):
            found.append(line[6:].split("\t", 1)[0])
        elif line.startswith(('--- "a/', '+++ "b/')):
            found.append(_unquote(line[4:].split("\t", 1)[0])[2:])
        else:
            found.extend(_unquote(line[len(prefix) :]) for prefix in _PATH_PREFIXES if line.startswith(prefix))
    return list(dict.fromkeys(found))


def _unreadable(patch: str) -> bool:
    """Report whether a patch holds a file header that :func:`patch_paths` cannot read.

    ``git apply`` strips any first path component and also applies plain
    ``---``/``+++`` diffs found between hunks, so a header with other prefixes
    or a header outside a ``diff --git`` section could change a hidden or
    protected file unseen (SKY-353). Hunk lines are counted, because a hunk
    body can hold lines that look like headers.

    Args:
        patch: ``git diff`` output.

    Returns:
        Whether any header is unreadable or stands outside a ``diff --git`` section.
    """
    state, old, new = "outside", 0, 0
    for section in re.split(r"(?m)^(?=diff --git )", patch):
        if section.startswith("diff --git "):
            header = section.split("\n", 1)[0][len("diff --git ") :]
            renamed = re.search(r"(?m)^(?:rename|copy) (?:from|to) ", section)
            if not _header_paths(header) and not (header.startswith(("a/", '"a/')) and renamed):
                return True
    for line in patch.split("\n"):
        if state == "hunk" and (old > 0 or new > 0):
            tag = line[:1] or " "
            if tag not in " -+\\":
                return True
            old -= tag in " -"
            new -= tag in " +"
            continue
        if line.startswith("diff --git "):
            state = "header"
        elif line.startswith(("--- ", "+++ ")):
            if state != "header" or not _FILE_LINE.match(line):
                return True
        elif line.startswith("GIT binary patch") and state == "header":
            state = "binary"
        elif (hunk := _HUNK.match(line)) and state in ("header", "hunk"):
            state, old, new = "hunk", int(hunk.group(1) or 1), int(hunk.group(2) or 1)
    return False


def inside(path: str, roots: Iterable[str]) -> bool:
    """Report whether ``path`` is one of ``roots`` or lies below one.

    Args:
        path: Repository-relative path.
        roots: Repository-relative files or folders; ``.`` is the whole tree.

    Returns:
        Whether a root covers the path.
    """
    return any(root == "." or path == root or path.startswith(root.rstrip("/") + "/") for root in roots)


def patch_violations(
    patch: str,
    editable_paths: Iterable[str],
    readonly_paths: Iterable[str] = (),
    protected_paths: Iterable[str] = (),
    exploit_paths: Iterable[str] = (),
) -> list[str]:
    """Explain every way a patch breaks the run's editing rules.

    A write under ``.optimize/exploits`` is always a violation, even when the
    editable paths cover it. So is a file at a parent of it, such as a
    ``.optimize`` file or symlink, because the suite could then not be added
    to the published patch. Case variants count, for case-insensitive disks.

    Args:
        patch: ``git diff`` output for one version.
        editable_paths: Files and folders the agent may change.
        readonly_paths: Submodules and Git LFS files, which stay as fetched.
        protected_paths: Files and folders the repository's constraints file
            protects; they win over the editable paths.
        exploit_paths: The configured ``[metric] exploits`` folder, when it
            differs from ``.optimize/exploits``. No caller passes it yet;
            SKY-356 part B passes it.

    Returns:
        One readable reason per broken rule; empty when the patch is allowed.
    """
    if len(patch.encode("utf-8")) > MAX_PATCH_BYTES:
        return [f"The change is larger than the {MAX_PATCH_BYTES // (1024 * 1024)} MiB limit."]
    editable, readonly, protected = list(editable_paths), list(readonly_paths), list(protected_paths)
    exploits = [root.casefold() for root in (EXPLOITS_DIR, *exploit_paths)]
    problems: list[str] = []
    in_header = False
    for line in patch.split("\n"):
        if line.startswith("diff --git "):
            in_header = True
        elif line.startswith(("@@", "GIT binary patch")):
            in_header = False
        mode = (_MODE_LINE.match(line) or _INDEX_MODE.match(line)) if in_header else None
        if mode and mode.group(1) == _GITLINK_MODE:
            problems.append("Submodules are read-only; the change adds, moves, or updates one.")
            break
    if _unreadable(patch):
        problems.append("The change has a file header Skynet cannot read, so it cannot check the files it touches.")
    for path in patch_paths(patch):
        parts = path.split("/")
        if path.startswith("/") or any(part in ("", ".", "..") for part in parts) or ".git" in parts:
            problems.append(f"'{path}' is not a path inside the repository.")
        elif inside(folded := path.casefold(), exploits) or any(root.startswith(folded + "/") for root in exploits):
            # Generic text: the proposer must not learn where the hidden attacks live (SKY-353).
            problems.append(f"'{path}' may not be changed.")
        elif inside(path, protected):
            problems.append(f"'{path}' is protected by the repository rules ([paths] protected).")
        elif inside(path, readonly):
            problems.append(f"'{path}' is a submodule or Git LFS file, which are read-only.")
        elif not inside(path, editable):
            problems.append(f"'{path}' is outside the editable paths.")
    return problems


def _member_path(name: str) -> str:
    """Normalize an archive member name to a repository-relative path.

    Args:
        name: Member name, which may start with ``./``.

    Returns:
        The path without a leading ``./`` or trailing ``/``.
    """
    return posixpath.normpath(name).lstrip("/") if name not in ("", ".", "./") else "."


def strip_archive(archive: Path, hidden_paths: Iterable[str], destination: Path) -> Path:
    """Pack a copy of a tree without the files the agent must never see.

    The metric files and the exploit folder stay in the trusted scorer's tree
    only, so the proposer cannot read the metric or the attacks on it (SKY-353).
    A hard link to a hidden file is dropped too, because it carries the file's
    bytes under another name.

    Args:
        archive: Packed full tree.
        hidden_paths: Repository-relative files and folders to leave out.
        destination: Where to write the stripped archive.

    Returns:
        ``destination``.
    """
    hidden = [_member_path(path) for path in hidden_paths]
    with tarfile.open(archive) as source, tarfile.open(destination, "w:gz") as target:
        for member in source:
            if inside(_member_path(member.name), hidden):
                continue
            if member.islnk() and inside(_member_path(member.linkname), hidden):
                continue
            target.addfile(member, source.extractfile(member) if member.isreg() else None)
    return destination


def drop_paths(patch: str, hidden_paths: Iterable[str]) -> str:
    """Remove the file sections of a patch that touch a hidden path.

    The scorer box always scores with the base commit's metric, so a version
    that writes a metric file or the exploit folder keeps the rest of its
    change and loses only that part (SKY-353).

    Args:
        patch: ``git diff`` output for one version.
        hidden_paths: Repository-relative files and folders the version may not change.

    Returns:
        The patch without those sections; unchanged when nothing is hidden.
    """
    hidden = list(hidden_paths)
    if not hidden or not patch:
        return patch
    sections = re.split(r"(?m)^(?=diff --git )", patch)
    return "".join(section for section in sections if not any(inside(path, hidden) for path in patch_paths(section)))


def archive_chunks(archive: Path) -> Iterator[str]:
    """Stream a packed tree as base64 text pieces for text-only sandbox uploads.

    Args:
        archive: Packed tree.

    Yields:
        Base64 text of consecutive slices; concatenated and decoded they
        rebuild the archive.
    """
    with archive.open("rb") as stream:
        while block := stream.read(ARCHIVE_CHUNK_BYTES):
            yield base64.b64encode(block).decode("ascii")


def _git(arguments: Sequence[str], cwd: Path) -> str:
    """Run git in a checkout and return its output.

    Args:
        arguments: Arguments after ``git``.
        cwd: Checkout root.

    Returns:
        Standard output.

    Raises:
        RuntimeError: When git fails.
    """
    result = subprocess.run(["git", *_GIT_IDENTITY, *arguments], cwd=cwd, capture_output=True, text=True, check=False)
    if result.returncode != 0:
        raise RuntimeError(f"git {arguments[0]} failed: {(result.stderr or result.stdout).strip()[-2000:]}")
    return result.stdout


def unpack_tree(chunks: Iterable[str | Path], destination: Path) -> Path:
    """Rebuild a shipped tree and commit it, so ``version_patch`` can diff against it.

    Args:
        chunks: Base64 chunk files in order.
        destination: Empty or missing folder the tree goes in.

    Returns:
        The destination.
    """
    destination.mkdir(parents=True, exist_ok=True)
    archive = destination.parent / f".{destination.name}.tgz"
    with archive.open("wb") as packed:
        for chunk in chunks:
            packed.write(base64.b64decode(Path(chunk).read_text(encoding="ascii")))
    with tarfile.open(archive) as tree:
        tree.extractall(destination, filter="data")
    archive.unlink()
    _git(["init", "--quiet"], destination)
    _git(["add", "--all", "--force"], destination)
    _git(["commit", "--quiet", "--allow-empty", "--no-verify", "-m", "base"], destination)
    _git(["update-ref", BASE_REF, "HEAD"], destination)
    return destination


def exclude_paths(checkout: Path, paths: Iterable[str]) -> None:
    """Keep files a proposer harness writes into a checkout out of every version.

    A harness's files can carry the proposer's credentials, so they must never
    reach a patch the parent scores or opens as a pull request.

    Args:
        checkout: Folder made by :func:`unpack_tree`; its worktrees share the exclusions.
        paths: Checkout-relative files the harness writes.

    Raises:
        ValueError: When a path is a file the repository tracks, which an exclusion cannot hide.
    """
    paths = [path.strip("/") for path in paths if path.strip("/")]
    if not paths:
        return
    tracked = _git(["ls-files", "--", *paths], checkout).splitlines()
    if tracked:
        raise ValueError(f"The proposer harness writes files the repository already has: {', '.join(tracked)}")
    exclude = Path(_git(["rev-parse", "--git-common-dir"], checkout).strip())
    exclude = (exclude if exclude.is_absolute() else checkout / exclude) / "info" / "exclude"
    exclude.parent.mkdir(parents=True, exist_ok=True)
    with exclude.open("a", encoding="utf-8") as stream:
        stream.write("".join(f"\n/{path}" for path in paths) + "\n")


def version_patch(checkout: Path) -> str:
    """Return everything changed in a checkout since ``unpack_tree`` as one patch.

    Files the repository ignores are left out, so caches and build output an
    agent leaves behind never bloat a version. Scoring rebuilds each version
    from its patch alone, so nothing left out can affect a score.

    Args:
        checkout: Folder made by :func:`unpack_tree`, possibly edited since.

    Returns:
        ``git diff --binary`` output; empty when nothing changed.
    """
    _git(["add", "--all"], checkout)
    # The agent can set git config in its checkout; other path prefixes would
    # hide which files a version touches, so the scorer would refuse it.
    pinned = ["-c", "diff.noprefix=false", "-c", "diff.mnemonicPrefix=false"]
    return _git(
        [
            *pinned,
            "diff",
            "--cached",
            "--binary",
            "--no-color",
            "--no-ext-diff",
            "--src-prefix=a/",
            "--dst-prefix=b/",
            BASE_REF,
        ],
        checkout,
    )


def reset_tree(checkout: Path) -> None:
    """Throw away every change in a checkout made by :func:`unpack_tree`.

    Args:
        checkout: Folder to restore to the shipped tree.
    """
    _git(["reset", "--quiet", "--hard", BASE_REF], checkout)
    _git(["clean", "--quiet", "-fdx"], checkout)


def apply_patch(checkout: Path, patch: str) -> None:
    """Apply a version to a clean checkout.

    Args:
        checkout: Folder made by :func:`unpack_tree`, reset to its base.
        patch: ``git diff --binary`` output.

    Raises:
        RuntimeError: When the patch does not apply.
    """
    if not patch.strip():
        return
    result = subprocess.run(
        ["git", "apply", "--binary", "--whitespace=nowarn", "-"],
        cwd=checkout,
        input=patch,
        capture_output=True,
        text=True,
        check=False,
    )
    if result.returncode != 0:
        raise RuntimeError(f"The change does not apply cleanly: {(result.stderr or result.stdout).strip()[-2000:]}")
