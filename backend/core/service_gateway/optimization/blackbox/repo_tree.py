"""Ship a repository tree into a sandbox and check the versions written against it.

A repository version is a ``git diff --binary`` against the run's starting
tree. Stdlib only: the native runner ships this module into its sandbox so
the agent's workspace and the trusted scorer apply the same rules.
"""

from __future__ import annotations

import base64
import logging
import posixpath
import re
import subprocess
import tarfile
import tempfile
import unicodedata
from collections.abc import Callable, Iterable, Iterator, Sequence
from pathlib import Path
from typing import Any

logger = logging.getLogger(__name__)

# Payload key naming the packed tree. The parent sets it to a file on its own
# disk; the sandbox supervisor uploads that file and rewrites the key into
# guest chunk paths. Secrets never ride in a payload, only their names do.
REPO_SNAPSHOT_KEY = "_repo_snapshot"
# Uploads are text and capped at 16 MiB per request: 9 MiB of bytes is 12 MiB of base64.
ARCHIVE_CHUNK_BYTES = 9 * 1024 * 1024
_GIT_IDENTITY = ["-c", "user.name=app", "-c", "user.email=app@localhost", "-c", "commit.gpgsign=false"]
# A stored tree may hold a folder shaped as a bare repository (``HEAD``,
# ``objects/``, ``refs/``, ``config``). Git would read that folder's config, and
# a filter, hook or fsmonitor in it would run code when git runs there. Every
# git call in the scorer, workspace, staging and engine runner paths that can
# run in a tree with no ``.git`` of its own sets this, so git refuses such a
# folder. The harness shell lines are out of scope: the agent runs its own code
# in that box anyway. A ``git -C repo`` call on a checkout with its
# own ``.git`` does not need it: git finds that ``.git`` first. Git reads
# ``safe.bareRepository`` only from protected config, so a repository's own
# config cannot turn it off (SKY-812).
SAFE_GIT = ("-c", "safe.bareRepository=explicit")

# A run with a constraints file answers every refused action with this fixed
# text, both in the sandbox and in the parent, and logs the reason as a
# ``rule.block`` run event instead. ``rule_judge`` and its tests use these.
ILLEGAL_ACTION_MESSAGE = (
    "Illegal action. Read the repository rules in your brief. You cannot do this action. Do not repeat it,"
    " because it wastes time."
)
RULE_BLOCK_EVENT = "rule.block"
PATH_RULE = "ascent.toml [paths]"


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
_SYMLINK_MODE = "120000"
_MODE_PREFIXES = ("new file mode", "deleted file mode", "old mode", "new mode", "index ")
# ``git apply`` reads a mode as any octal number and ignores what follows a
# space, so ``0120000`` or ``120644`` also make a link. Git writes only these
# four modes, so any other mode line is refused as unreadable (SKY-906).
_MODE_LINE = re.compile(r"^(?:new file mode|deleted file mode|old mode|new mode) (100644|100755|120000|160000)\s*$")
_INDEX_MODE = re.compile(r"^index [0-9a-f]+\.\.[0-9a-f]+(?: (100644|100755|120000|160000))?\s*$")
# ``git apply`` still reads the old ``rename old``/``rename new`` spelling, so
# a hand-written patch could move a file past every path rule (SKY-906).
_PATH_PREFIXES = ("rename from ", "rename to ", "rename old ", "rename new ", "copy from ", "copy to ")
_HUNK = re.compile(r"^@@ -\d+(?:,(\d+))? \+\d+(?:,(\d+))? @@")
# Git decodes only these escapes in a quoted name (quote.c, unquote_c_style).
_GIT_QUOTED = re.compile(r'"(?:[^"\\]|\\[abfnrtv"\\]|\\[0-3][0-7]{2})*"')
_FILE_LINE = re.compile(r'^(?:--- (?:a/|"a/|/dev/null)|\+\+\+ (?:b/|"b/|/dev/null))')
# A model writes plain diffs and their hunk headers, so the counts are
# untrusted: the digits are bounded.
HUNK_HEADER = re.compile(r"^@@ -\d{1,9}(?:,(\d{1,9}))? \+\d{1,9}(?:,(\d{1,9}))? @@", re.MULTILINE)
# ``git apply`` ends a ``diff --git`` header at the first line it does not
# know and then reads a later ``---``/``+++`` pair as a plain diff, which cuts
# a trailing date off the name. Git keeps reading the header only at these
# lines; ``Binary files`` and ``GIT binary patch`` end it and start the binary
# part (SKY-906).
_HEADER_LINES = (
    "old mode ",
    "new mode ",
    "deleted file mode ",
    "new file mode ",
    "similarity index ",
    "dissimilarity index ",
    "index ",
    "--- ",
    "+++ ",
    "Binary files ",
    "GIT binary patch",
    "@@ -",
    *_PATH_PREFIXES,
)


def _unquote(token: str) -> str:
    """Decode a C-quoted git path, octal escapes included.

    Args:
        token: Path as git printed it, with or without surrounding quotes.

    Returns:
        The path's text.

    Raises:
        ValueError: A quoted token that git cannot decode.
    """
    if not (len(token) >= 2 and token.startswith('"') and token.endswith('"')):
        return token
    if not _GIT_QUOTED.fullmatch(token):
        # Git reads a name with any other escape, such as ``\x73``, as raw text
        # with its quotes, so Python's wider decode would check another path (SKY-906).
        raise ValueError(f"git cannot decode the quoted name {token!r}")
    if any(ord(char) > 127 for char in token):
        # Git reads raw bytes between the quotes as they are, and the decode
        # below would read them as Latin-1 (SKY-906).
        raise ValueError(f"git reads the quoted name {token!r} another way")
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

    Raises:
        ValueError: A quoted name that git cannot decode.
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


def _plain_name(value: str) -> bool:
    """Report whether ``git apply`` reads a header path as all of ``value``.

    Git ends an unquoted name at a carriage return and ignores any text after
    a quoted name's closing quote, so ``secret.txt\\r`` or ``"secret.txt" x``
    would move a protected file that the path rules never see (SKY-906).

    Args:
        value: A rename or copy line after its ``rename from ``-style prefix,
            or a ``---``/``+++`` name with its ``a/`` or ``b/`` prefix.

    Returns:
        Whether the whole value is one quoted name, or a bare name with no
        control characters.
    """
    if value.startswith('"'):
        return len(value) >= 2 and value.endswith('"') and _split_quoted(value) == [value]
    return not any(ord(char) < 32 or ord(char) == 127 for char in value)


def _misread(path: str) -> bool:
    """Report whether git writes another name than the decoded ``path``.

    A name that is not UTF-8 decodes to U+FFFD, and git ends a quoted name at
    a ``\\000`` byte. The path rules and the link checks would then test
    another name than git writes (SKY-906).

    Args:
        path: A path that :func:`patch_paths` decoded.

    Returns:
        Whether the path holds U+FFFD or a NUL.
    """
    return "\ufffd" in path or "\x00" in path


def _plain_header_path(old: str, new: str) -> tuple[str, str] | None:
    """Read the one path and the mode line of a plain ``---``/``+++`` header pair.

    Args:
        old: The ``---`` line after its marker.
        new: The ``+++`` line after its marker.

    Returns:
        The path and the mode line it needs (empty for a changed file), or
        ``None`` when the pair names no single ``a/``/``b/`` path.
    """
    old, new = old.split("\t", 1)[0], new.split("\t", 1)[0]
    # git apply drops a CR, end whitespace or a date after a space, which the
    # path checks keep: a CRLF ``a/p.py\r`` would pass a rule on ``p.py`` and
    # still change it. So a path with any whitespace gets no repair.
    if any(ord(char) < 32 or ord(char) == 127 or char.isspace() for char in old + new):
        return None
    before = None if old == "/dev/null" else old[2:] if old.startswith("a/") else ""
    after = None if new == "/dev/null" else new[2:] if new.startswith("b/") else ""
    if before is None and after:
        return after, "new file mode 100644"
    if after is None and before:
        # A plain diff gives no mode, so git refuses to delete a link or a
        # submodule with this line ("wrong type"): that fails safe.
        return before, "deleted file mode 100644"
    if before and before == after:
        return before, ""
    return None


def with_git_headers(patch: str) -> str:
    """Give each file of a plain unified diff the ``diff --git`` line that the path check needs.

    Models often write ``--- a/X``/``+++ b/X`` with no ``diff --git`` line, and
    the SKY-353 check refuses any header outside a ``diff --git`` section.
    Hunk lines are counted, so a hunk line that looks like a header gets none.

    Args:
        patch: A unified diff.

    Returns:
        The diff with a ``diff --git`` line (and a mode line for a new or
        deleted file) before each header pair; unchanged when it already has a
        ``diff --git`` line, any pair names no single ``a/``/``b/`` path or has
        no hunk right after it, or a hunk line is broken.
    """
    if re.search(r"(?m)^diff --git ", patch):
        return patch
    lines = patch.split("\n")
    out: list[str] = []
    old = new = 0
    for index, line in enumerate(lines):
        if old > 0 or new > 0:
            tag = line[:1] or " "
            if tag not in " -+\\":
                return patch
            old -= tag in " -"
            new -= tag in " +"
        elif line.startswith("--- ") and index + 1 < len(lines) and lines[index + 1].startswith("+++ "):
            header = _plain_header_path(line[4:], lines[index + 1][4:])
            # A mode line after the pair would follow the inserted one, and git
            # takes the last: ``new file mode 120000`` would then make a link.
            if header is None or index + 2 >= len(lines) or not HUNK_HEADER.match(lines[index + 2]):
                return patch
            path, mode = header
            out.extend([f"diff --git a/{path} b/{path}", *([mode] if mode else [])])
        # A header with more digits than this regex takes is not a hunk here,
        # so the repair may mark lines git reads as hunk lines: git then calls
        # the patch corrupt, and the checker sees every inserted path.
        elif hunk := HUNK_HEADER.match(line):
            old = 1 if hunk.group(1) is None else int(hunk.group(1))
            new = 1 if hunk.group(2) is None else int(hunk.group(2))
        out.append(line)
    return "\n".join(out)


def _unreadable(patch: str) -> bool:
    """Report whether a patch holds a file header that :func:`patch_paths` cannot read.

    ``git apply`` strips any first path component and also applies plain
    ``---``/``+++`` diffs found between hunks, so a header with other prefixes
    or a header outside a ``diff --git`` section could change a hidden or
    protected file unseen (SKY-353). Hunk lines are counted, because a hunk
    body can hold lines that look like headers. A header line that git does
    not know also counts, because git ends the header there.

    Args:
        patch: ``git diff`` output.

    Returns:
        Whether any header is unreadable or stands outside a ``diff --git`` section.
    """
    state, old, new = "outside", 0, 0
    for section in re.split(r"(?m)^(?=diff --git )", patch):
        if section.startswith("diff --git "):
            header = section.split("\n", 1)[0][len("diff --git ") :]
            renamed = re.search(r"(?m)^(?:rename (?:from|to|old|new)|copy (?:from|to)) ", section)
            if not _header_paths(header) and not (header.startswith(("a/", '"a/')) and renamed):
                return True
    lines = patch.split("\n")
    for number, line in enumerate(lines):
        if state == "hunk" and (old > 0 or new > 0):
            tag = line[:1] or " "
            if tag not in " -+\\":
                return True
            old -= tag in " -"
            new -= tag in " +"
            continue
        if line.startswith("diff --git "):
            state = "header"
        elif state == "header" and (
            (not line.startswith(_HEADER_LINES) and (line or number < len(lines) - 1))
            or (line.startswith("@@") and not _HUNK.match(line))
        ):
            # Git also ends the header at a broken ``@@`` line (SKY-906).
            return True
        elif state == "header" and (prefix := next((p for p in _PATH_PREFIXES if line.startswith(p)), None)):
            if not _plain_name(line[len(prefix) :]):
                return True
        elif line.startswith(("--- ", "+++ ")):
            # Git also ends a bare name here at a carriage return (SKY-906).
            if state != "header" or not _FILE_LINE.match(line) or not _plain_name(line[4:].split("\t", 1)[0]):
                return True
        elif line.startswith(("GIT binary patch", "Binary files ")) and state == "header":
            # Git ends the header at ``Binary files``, and its own output has
            # no file line after it (SKY-906).
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
    A symlink that the patch adds, makes from a file, or retargets is a
    violation in every folder; deleting one is allowed.

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
    gitlink = symlink = odd_mode = False
    in_header = old_link = new_mode = False
    for line in patch.split("\n"):
        if line.startswith("diff --git "):
            # With no ``new mode`` line, ``git apply`` gives the new side the old
            # mode, so ``old mode 120000`` alone retargets or moves a link.
            symlink = symlink or (old_link and not new_mode)
            in_header, old_link, new_mode = True, False, False
        elif line.startswith(("@@", "GIT binary patch")):
            in_header = False
        if not in_header or not line.startswith(_MODE_PREFIXES):
            continue
        mode = _MODE_LINE.match(line) or _INDEX_MODE.match(line)
        odd_mode = odd_mode or mode is None
        gitlink = gitlink or bool(mode and mode.group(1) == _GITLINK_MODE)
        old_link = old_link or bool(mode and mode.group(1) == _SYMLINK_MODE and line.startswith("old mode"))
        new_mode = new_mode or line.startswith("new mode")
        # Only a link's new side can point outside the tree, so deleting a link
        # or turning one into a file stays allowed (SKY-906). An index line names
        # the mode only when both sides share it, so 120000 there retargets a link.
        symlink = symlink or bool(
            mode and mode.group(1) == _SYMLINK_MODE and not line.startswith(("deleted file mode", "old mode"))
        )
    symlink = symlink or (old_link and not new_mode)
    if gitlink:
        problems.append("Submodules are read-only; the change adds, moves, or updates one.")
    if symlink:
        problems.append("Symlinks are not allowed; the change adds a symlink, turns a file into one, or retargets one.")
    try:
        paths: list[str] | None = patch_paths(patch)
    except ValueError:
        paths = None
    if odd_mode or paths is None or _unreadable(patch) or any(map(_misread, paths)):
        problems.append("The change has a file header Skynet cannot read, so it cannot check the files it touches.")
    for path in paths or []:
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


def case_insensitive(directory: Path) -> bool:
    """Report whether the file system at ``directory`` finds a file under another case.

    On such a disk, as on macOS in local mode, ``secret.txt`` opens
    ``Secret.txt``, so a hidden path must match its case variants too.

    Args:
        directory: An existing folder on the disk to probe.

    Returns:
        Whether an upper-case name finds a probe file created in lower case.
    """
    with tempfile.NamedTemporaryFile(dir=directory, prefix="case-probe-") as probe:
        return (directory / Path(probe.name).name.upper()).exists()


def fold_case(ignore_case: bool) -> Callable[[str], str]:
    """Return the function that makes two paths compare equal on the disk.

    Every path is put in Unicode NFC form first, because macOS stores and
    opens a name in NFC and NFD form as one file, so ``café`` in either
    form is the same hidden path (SKY-446).

    Args:
        ignore_case: Whether the disk ignores case, from :func:`case_insensitive`.

    Returns:
        A function that returns the NFC form of a path, also case-folded
        when the disk ignores case.
    """
    if ignore_case:
        return lambda path: unicodedata.normalize("NFC", unicodedata.normalize("NFD", path).casefold())
    return lambda path: unicodedata.normalize("NFC", path)


def strip_archive(archive: Path, hidden_paths: Iterable[str], destination: Path) -> Path:
    """Pack a copy of a tree without the files the agent must never see.

    The metric files and the exploit folder stay in the trusted scorer's tree
    only, so the proposer cannot read the metric or the attacks on it (SKY-353).
    A hard link to a hidden file is dropped too, because it carries the file's
    bytes under another name. On a disk that ignores case, a case variant of
    a hidden path is dropped too, because the scorer reads it under the
    hidden name (SKY-446). A name in another Unicode form (NFC or NFD) is
    dropped on every disk.

    Args:
        archive: Packed full tree.
        hidden_paths: Repository-relative files and folders to leave out.
        destination: Where to write the stripped archive.

    Returns:
        ``destination``.
    """
    fold = fold_case(case_insensitive(destination.parent))
    hidden = [fold(_member_path(path)) for path in hidden_paths]
    with tarfile.open(archive) as source, tarfile.open(destination, "w:gz") as target:
        for member in source:
            if inside(fold(_member_path(member.name)), hidden):
                continue
            if member.islnk() and inside(fold(_member_path(member.linkname)), hidden):
                continue
            target.addfile(member, source.extractfile(member) if member.isreg() else None)
    return destination


def extract_folder(archive: Path, folder: str, destination: Path) -> Path:
    """Unpack one folder of a packed tree, such as a Dockerfile's build context (SKY-737).

    Args:
        archive: Packed full tree.
        folder: Repository-relative folder; ``.`` is the whole tree.
        destination: Empty or missing folder the files go in, at their repository paths.

    Returns:
        The folder's path under ``destination``.
    """
    root = _member_path(folder)
    destination.mkdir(parents=True, exist_ok=True)
    with tarfile.open(archive) as tree:
        members = [member for member in tree if inside(_member_path(member.name), (root,))]
        tree.extractall(destination, members=members, filter="data")
    target = destination / root
    target.mkdir(parents=True, exist_ok=True)
    return target


def linked_hidden_paths(archive: Path, hidden_paths: Iterable[str]) -> list[str]:
    """Find the hidden paths that a link could carry into the agent's tree.

    The strip matches member names. A hidden path that is a symlink, or lies
    below one, loses only the link, and its target with the scoring files
    stays. A symlink inside a hidden folder lets the scorer read a file
    outside it, possibly outside the tree. A hard link inside a hidden folder
    to a file outside it leaves the same bytes under the visible name. On a
    disk that ignores case, names compare without case, as in the strip.

    Args:
        archive: Packed full tree.
        hidden_paths: Repository-relative files and folders the agent must not see.

    Returns:
        Each hidden path that is, holds or lies below such a link, in order.
    """
    fold = fold_case(case_insensitive(archive.parent))
    hidden = [_member_path(path) for path in hidden_paths]
    folded = [fold(path) for path in hidden]
    symlinks: list[str] = []
    linked: set[str] = set()
    with tarfile.open(archive) as source:
        for member in source:
            name = fold(_member_path(member.name))
            if member.issym():
                symlinks.append(name)
                linked.update(path for path in folded if inside(name, [path]))
            elif member.islnk() and not inside(fold(_member_path(member.linkname)), folded):
                linked.update(path for path in folded if inside(name, [path]))
    return [path for path in hidden if fold(path) in linked or inside(fold(path), symlinks)]


def archive_links(archive: Path) -> frozenset[str]:
    """List the symlinks of a packed tree.

    Args:
        archive: Packed tree.

    Returns:
        The repository-relative path of every symlink member.
    """
    with tarfile.open(archive) as source:
        return frozenset(_member_path(member.name) for member in source if member.issym())


def link_edits(patch: str, links: Iterable[str]) -> list[str]:
    """List the shipped symlinks that a patch retargets, moves, or copies.

    A section with no mode takes the mode from the checkout, so a plain hunk,
    a rename or a copy of a shipped link keeps a link, with the new target or
    at the new name (SKY-906). A section that deletes the link is allowed,
    and so is a new file at its path: git writes a link that becomes a file
    as a deletion and a new file, and :func:`patch_violations` already
    refuses a new link.

    Args:
        patch: ``git diff`` output that :func:`patch_violations` allows.
        links: Repository-relative paths of the symlinks in the shipped tree.

    Returns:
        Each touched symlink path, in first-seen order.

    Raises:
        ValueError: A quoted name that git cannot decode.
    """
    shipped = set(links)
    found: list[str] = []
    for section in re.split(r"(?m)^(?=diff --git )", patch):
        header = re.split(r"(?m)^(?:@@|GIT binary patch)", section, maxsplit=1)[0]
        if section.startswith("diff --git ") and not re.search(r"(?m)^(?:deleted|new) file mode ", header):
            found.extend(path for path in patch_paths(section) if path in shipped)
    return list(dict.fromkeys(found))


def drop_paths(patch: str, hidden_paths: Iterable[str]) -> str:
    """Remove the file sections of a patch that touch a hidden path.

    The scorer box always scores with the base commit's metric, so a version
    that writes a metric file or the exploit folder keeps the rest of its
    change and loses only that part (SKY-353).

    Args:
        patch: ``git diff`` output for one version.
        hidden_paths: Repository-relative files and folders the version may not change.

    Returns:
        The patch without those sections; unchanged when nothing is hidden;
        empty when a section deletes a hidden file.
    """
    hidden = list(hidden_paths)
    if not hidden or not patch:
        return patch
    sections = re.split(r"(?m)^(?=diff --git )", patch)
    dropped = [section for section in sections if _touches(section, hidden)]
    # ``version_patch`` writes a move as a delete plus a new file, so the new
    # file can carry the metric's text under a visible name (SKY-906).
    if any(re.search(r"(?m)^deleted file mode ", section) for section in dropped):
        logger.warning("Served patch dropped: it deletes a hidden file, so a new file may hold its text")
        return ""
    return "".join(section for section in sections if section not in dropped)


def _touches(section: str, hidden: list[str]) -> bool:
    """Report whether one patch section may change a hidden path.

    Args:
        section: One ``diff --git`` section, or the text before the first one.
        hidden: Repository-relative files and folders the version may not change.

    Returns:
        Whether a path of the section lies under ``hidden``, or a quoted name
        in it is one that git cannot decode, so the section could write any path.
    """
    try:
        paths = patch_paths(section)
    except ValueError:
        return True
    return any(inside(path, hidden) for path in paths)


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
    result = subprocess.run(
        ["git", *SAFE_GIT, *_GIT_IDENTITY, *arguments], cwd=cwd, capture_output=True, text=True, check=False
    )
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
    # A rename entry names no file mode, so a moved link would hide from the
    # symlink check; ``--no-renames`` writes it as a new 120000 file (SKY-906).
    # The cost: a moved file now counts twice against MAX_PATCH_BYTES.
    pinned = ["-c", "diff.noprefix=false", "-c", "diff.mnemonicPrefix=false"]
    return _git(
        [
            *pinned,
            "diff",
            "--cached",
            "--binary",
            "--no-color",
            "--no-ext-diff",
            "--no-renames",
            "--src-prefix=a/",
            "--dst-prefix=b/",
            BASE_REF,
        ],
        checkout,
    )


def forget_file_stats(checkout: Path) -> None:
    """Reload the index from the shipped tree so git hashes every file again.

    Use it after files were replaced in place with the same mtime. Linux can
    reuse the freed inode, so a same-size edit keeps the stat that git cached,
    and ``git add`` skips it (SKY-843).

    Args:
        checkout: Folder made by :func:`unpack_tree`.
    """
    _git(["read-tree", BASE_REF], checkout)


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
        RuntimeError: When the patch has a header Skynet cannot read, does
            not apply, or leaves a symlink at a path it touches.
    """
    if not patch.strip():
        return
    # A hardening fix can be a plain diff, which the header check refuses.
    patch = with_git_headers(patch)
    # The check after the apply trusts the paths read here, so a header that
    # git reads another way must stop the apply first (SKY-906).
    try:
        paths = patch_paths(patch)
    except ValueError as error:
        raise RuntimeError("The change has a file header Skynet cannot read.") from error
    if _unreadable(patch) or any(map(_misread, paths)):
        raise RuntimeError("The change has a file header Skynet cannot read.")
    result = subprocess.run(
        ["git", *SAFE_GIT, "apply", "--binary", "--whitespace=nowarn", "-"],
        cwd=checkout,
        input=patch,
        capture_output=True,
        text=True,
        check=False,
    )
    if result.returncode != 0:
        raise RuntimeError(f"The change does not apply cleanly: {(result.stderr or result.stdout).strip()[-2000:]}")
    # A section with no mode takes the mode from the checkout, so a plain hunk
    # or rename can retarget or move a link the shipped tree already has. Only
    # the applied tree shows that (SKY-906).
    linked = [path for path in paths if (checkout / path).is_symlink()]
    for path in linked:
        (checkout / path).unlink()
    if linked:
        raise RuntimeError(f"Symlinks are not allowed; the change leaves one at {linked[0]!r}.")
