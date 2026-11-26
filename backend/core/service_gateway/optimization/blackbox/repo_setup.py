"""Infer the shell command that prepares a repository checkout for scoring.

Users never type a setup command for a repository run: the trusted parent
reads the fetched tree, asks the run's optimization model when one is
reachable, and otherwise falls back to the manifests and lockfiles it finds.
The model can also name Debian system packages, which staging installs in a
generated ``[run]`` image, because the setup command never runs as root (SKY-967).
"""

from __future__ import annotations

import fnmatch
import logging
import re
import tarfile
from collections.abc import Callable, Iterable
from dataclasses import dataclass
from pathlib import Path, PurePosixPath

logger = logging.getLogger(__name__)

# A model reads these, so each is capped; listing and contents stay well under
# any context window while still showing what the repository is built with.
_MAX_LISTED_PATHS = 400
_MAX_FILE_CHARS = 4_000
_MAX_TOTAL_CHARS = 40_000
_MAX_COMMAND_CHARS = 300
# Debian policy 5.6.1: lowercase letters, digits, "+", "-" and "."; at least two
# characters, starting with a letter or digit. The names go into a Dockerfile
# RUN line, so nothing else may pass.
_APT_PACKAGE = re.compile(r"[a-z0-9][a-z0-9+.-]+")
_APT_PREFIX = "apt:"
_KEY_FILE_PATTERNS = (
    "README*",
    ".github/workflows/*.yml",
    ".github/workflows/*.yaml",
    "pyproject.toml",
    "requirements*.txt",
    "setup.py",
    "setup.cfg",
    "package.json",
    "Makefile",
    "Cargo.toml",
    "go.mod",
    "Dockerfile",
    "tox.ini",
    "noxfile.py",
)
# Lockfiles are large and only their presence matters.
_PRESENCE_ONLY = ("uv.lock", "poetry.lock", "package-lock.json", "pnpm-lock.yaml", "yarn.lock", "Cargo.lock", "go.sum")
# First match wins: a lockfile names the tool the project already pins.
_DETECTORS = (
    (("uv.lock",), "uv sync --frozen"),
    (("poetry.lock",), "poetry install --no-interaction"),
    (("requirements.txt",), "pip install -r requirements.txt"),
    (("pyproject.toml", "setup.py"), "pip install -e ."),
    (("package-lock.json",), "npm ci"),
    (("pnpm-lock.yaml",), "pnpm install --frozen-lockfile"),
    (("yarn.lock",), "yarn install --frozen-lockfile"),
    (("Cargo.toml",), "cargo build"),
    (("go.mod",), "go build ./..."),
)
_PROMPT = """You prepare a freshly cloned repository so its code can be run and tested.
Below are the repository's file listing and its key files.
Reply with exactly one line: a single shell command, run from the repository root,
that installs its dependencies and builds it. Prefer the project's own lockfile and
tooling (as its CI does). Do not run tests, start servers or use sudo.
If nothing needs installing, reply with exactly: none
If the repository also needs Debian system packages (for example a C library or
a compiler), add a second line: apt: <package names separated by spaces>
Never put apt-get or sudo in the command: the apt line installs those packages.

File listing:
{listing}

Key files:
{files}
"""


@dataclass(frozen=True)
class SetupPlan:
    """The setup command of a repository and the Debian packages it needs (SKY-967)."""

    command: str | None
    apt_packages: tuple[str, ...] = ()


@dataclass(frozen=True)
class RepoOverview:
    """The parts of a repository tree that reveal how it is set up."""

    paths: tuple[str, ...]
    files: dict[str, str]


def _is_key_file(path: str) -> bool:
    """Report whether a path is one of the files worth showing the model.

    Args:
        path: Repository-relative POSIX path.

    Returns:
        Whether it matches a key-file pattern; only top-level files and CI
        workflows qualify, since ``*`` would otherwise cross folders.
    """
    folder = path.rpartition("/")[0]
    if folder not in ("", ".github/workflows"):
        return False
    return any(fnmatch.fnmatchcase(path, pattern) for pattern in _KEY_FILE_PATTERNS)


def read_overview(archive: Path) -> RepoOverview:
    """Read the file listing and key files out of a packed snapshot.

    Args:
        archive: Packed tree from :func:`fetch_snapshot`.

    Returns:
        Every regular file's path and the capped text of each key file.
    """
    paths: list[str] = []
    files: dict[str, str] = {}
    total = 0
    with tarfile.open(archive, "r:gz") as packed:
        members = sorted((member for member in packed.getmembers() if member.isfile()), key=lambda m: m.name)
        for member in members:
            path = str(PurePosixPath(member.name))
            paths.append(path)
            if not _is_key_file(path) or total >= _MAX_TOTAL_CHARS:
                continue
            handle = packed.extractfile(member)
            if handle is None:
                continue
            text = handle.read(_MAX_FILE_CHARS * 4).decode("utf-8", errors="replace")[:_MAX_FILE_CHARS]
            files[path] = text
            total += len(text)
    return RepoOverview(paths=tuple(paths), files=files)


def detect_setup_command(paths: Iterable[str]) -> str | None:
    """Pick a setup command from the manifests and lockfiles at the repository root.

    Args:
        paths: Repository-relative file paths.

    Returns:
        The install command for the first recognized project kind, or ``None``.
    """
    present = set(paths)
    for markers, command in _DETECTORS:
        if any(marker in present for marker in markers):
            return command
    return None


def _prompt(overview: RepoOverview) -> str:
    """Write the question the model answers.

    Args:
        overview: Listing and key files of the repository.

    Returns:
        The full prompt.
    """
    listed = list(overview.paths[:_MAX_LISTED_PATHS])
    if len(overview.paths) > _MAX_LISTED_PATHS:
        listed.append(f"... and {len(overview.paths) - _MAX_LISTED_PATHS} more files")
    present = [name for name in _PRESENCE_ONLY if name in overview.paths]
    files = "\n\n".join(f"--- {path} ---\n{text}" for path, text in overview.files.items())
    if present:
        files += "\n\nLockfiles present: " + ", ".join(present)
    return _PROMPT.format(listing="\n".join(listed), files=files or "(none)")


def apt_packages(names: Iterable[str]) -> tuple[str, ...]:
    """Keep only valid Debian package names, without duplicates and in sorted order.

    Args:
        names: Package names from the model, or from a stored payload.

    Returns:
        The valid names, sorted, each once.
    """
    names = list(names)
    kept = [name for name in names if isinstance(name, str) and _APT_PACKAGE.fullmatch(name)]
    if dropped := len(names) - len(kept):
        logger.info("Dropped %d invalid Debian package names from the setup answer", dropped)
    return tuple(sorted(set(kept)))


def parse_answer(answer: str) -> SetupPlan | None:
    """Read the model's answer: one command line and at most one ``apt:`` line.

    Args:
        answer: Raw model reply.

    Returns:
        The plan (its command is ``None`` when the model said nothing needs
        setting up), or ``None`` when the reply is unusable.
    """
    lines = [line.strip().strip("`").strip() for line in answer.strip().strip("`").splitlines()]
    lines = [line for line in lines if line and line.lower() not in {"bash", "sh", "shell"}]
    apt_lines = [line for line in lines if line.lower().startswith(_APT_PREFIX)]
    lines = [line for line in lines if line not in apt_lines]
    if len(lines) != 1 or len(apt_lines) > 1:
        return None
    packages: tuple[str, ...] = ()
    if apt_lines:
        names = apt_lines[0][len(_APT_PREFIX) :].replace(",", " ").split()
        packages = () if [name.lower() for name in names] == ["none"] else apt_packages(names)
    command = lines[0].removeprefix("$ ").strip()
    if command.lower().rstrip(".") == "none":
        return SetupPlan(None, packages)
    if len(command) > _MAX_COMMAND_CHARS or "\x00" in command:
        return None
    return SetupPlan(command, packages)


def infer_setup_command(archive: Path, ask: Callable[[str], str | None] | None = None) -> SetupPlan:
    """Infer the command that prepares a checkout of the snapshot, and its Debian packages.

    Args:
        archive: Packed tree from :func:`fetch_snapshot`.
        ask: Sends a prompt to the run's model and returns its reply, or
            ``None`` when the call did not succeed; without it only the
            manifests decide.

    Returns:
        The plan; its command is ``None`` when the repository needs no setup.
        Only the model names packages, so the manifest fallback has none.
    """
    overview = read_overview(archive)
    fallback = SetupPlan(detect_setup_command(overview.paths))
    if ask is None:
        return fallback
    try:
        answer = ask(_prompt(overview))
    except Exception:  # isolation boundary: the manifest fallback must still set the run up
        logger.warning("Asking the model for a repository setup command failed", exc_info=True)
        return fallback
    plan = parse_answer(answer or "")
    if plan is None:
        logger.info("The model's setup command was unusable; using the manifest fallback")
        return fallback
    return plan
