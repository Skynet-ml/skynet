"""Fetch a hosted repository at one commit and pack its tree for a sandboxed run.

Runs only in the trusted parent: sandboxes have no network, so the parent
clones with the user's GitHub, GitLab or Bitbucket connection and ships the
tree in as text. The
token travels through git's environment config, never its argument list, so
it stays out of process listings and error messages.
"""

from __future__ import annotations

import base64
import os
import shutil
import subprocess
import tarfile
import zipfile
from dataclasses import dataclass
from pathlib import Path
from typing import Any
from urllib.parse import quote

from ..api.errors import DomainError
from .github import API_URL, PROVIDER, _headers, _repo_url, _split_repo, oauth_app
from .oauth import current_oauth_token
from .transport import get_json
from .vault import ConnectorVault

GITHUB_HOST = "https://github.com"
# Clone origin and the basic-auth user name each host documents for a token over HTTPS.
GIT_HOSTS = {
    "github": (GITHUB_HOST, "x-access-token"),
    "gitlab": ("https://gitlab.com", "oauth2"),
    "bitbucket": ("https://bitbucket.org", "x-token-auth"),
}
# Compressed tree ceiling: the tree crosses into each sandbox as base64 text.
MAX_ARCHIVE_BYTES = 200 * 1024 * 1024
# The commit picker shows only recent work; an older commit is rarely the start of a run.
COMMIT_LIMIT = 20
COMMIT_TITLE_MAX = 120
_GIT_TIMEOUT_SECONDS = 900.0


class RepoFetchError(RuntimeError):
    """Explain, in words a run's owner can act on, why the repository could not be fetched."""


class RepoRateLimitError(RepoFetchError):
    """GitHub's rate limit stopped the lookup, so the repository itself may be readable."""


@dataclass(frozen=True)
class RepoSnapshot:
    """A repository tree fetched at one commit, ready to ship into a sandbox."""

    commit: str
    archive: Path
    readonly_paths: tuple[str, ...]
    size_bytes: int


def github_token(engine: Any, username: str) -> str:
    """Return a usable token from the user's GitHub connection.

    Args:
        engine: SQLAlchemy engine holding the connector vault.
        username: Owner of the connection.

    Returns:
        The bearer token, refreshed when an OAuth token went stale.

    Raises:
        RepoFetchError: When GitHub is not connected or the link expired.
    """
    try:
        return current_oauth_token(oauth_app(), ConnectorVault(engine), username)
    except DomainError as error:
        raise RepoFetchError("Connect GitHub again: the connection is missing or has expired.") from error


def resolve_commit(token: str | None, repository: str, branch: str | None) -> str:
    """Pin a branch, or the default branch, to its current commit.

    Args:
        token: GitHub token with read access to the repository; ``None``
            calls anonymously, which reaches public repositories only.
        repository: ``owner/name``.
        branch: Branch to pin; ``None`` picks the repository's default branch.

    Returns:
        The 40-character commit id.

    Raises:
        RepoRateLimitError: When GitHub's rate limit stops the lookup.
        RepoFetchError: When GitHub refuses the lookup.
    """
    headers = _headers(token)
    try:
        if branch is None:
            branch = str(
                get_json(f"{API_URL}/repos/{repository}", provider=PROVIDER, headers=headers)["default_branch"]
            )
        url = f"{API_URL}/repos/{repository}/commits/{quote(branch, safe='')}"
        return str(get_json(url, provider=PROVIDER, headers=headers)["sha"])
    except DomainError as error:
        if error.code == "connectors.rate_limited":
            raise RepoRateLimitError(
                "GitHub's rate limit is reached. Try again later. Connecting GitHub raises the limit."
            ) from error
        raise RepoFetchError(f"GitHub could not find {repository} at '{branch or 'its default branch'}'.") from error


def commit_summary(sha: Any, message: Any, date: Any) -> dict[str, Any] | None:
    """Shape one commit for the commit picker.

    Args:
        sha: The commit id from the host.
        message: The full commit message.
        date: The commit time as an ISO 8601 string.

    Returns:
        ``{"sha", "title", "date"}`` with the message's first line, or
        ``None`` when the host sent no usable id.
    """
    if not isinstance(sha, str) or not sha:
        return None
    title = message.strip().split("\n", 1)[0].strip() if isinstance(message, str) else ""
    return {"sha": sha, "title": title[:COMMIT_TITLE_MAX], "date": date if isinstance(date, str) else None}


def list_commits(token: str | None, repository: str, branch: str) -> list[dict[str, Any]]:
    """List the newest commits of a GitHub branch, newest first.

    Args:
        token: GitHub token with read access to the repository.
        repository: ``owner/name``.
        branch: Branch name; empty for the default branch.

    Returns:
        Up to :data:`COMMIT_LIMIT` summaries, see :func:`commit_summary`.
    """
    owner, name = _split_repo(repository)
    params: dict[str, Any] = {"per_page": COMMIT_LIMIT}
    if branch.strip():
        params["sha"] = branch.strip()
    body = get_json(f"{_repo_url(owner, name)}/commits", provider=PROVIDER, headers=_headers(token), params=params)
    commits = []
    for item in body if isinstance(body, list) else []:
        detail = item.get("commit") if isinstance(item, dict) else None
        detail = detail if isinstance(detail, dict) else {}
        committer = detail.get("committer") if isinstance(detail.get("committer"), dict) else {}
        summary = commit_summary(
            item.get("sha") if isinstance(item, dict) else None, detail.get("message"), committer.get("date")
        )
        if summary:
            commits.append(summary)
    return commits[:COMMIT_LIMIT]


def git_host(provider: str) -> tuple[str, str]:
    """Look up the clone origin and git user name of a repository host.

    Args:
        provider: ``github``, ``gitlab`` or ``bitbucket``.

    Returns:
        ``(origin, user)``.

    Raises:
        RepoFetchError: When the provider is not a repository host.
    """
    try:
        return GIT_HOSTS[provider]
    except KeyError as error:
        raise RepoFetchError(f"'{provider}' is not a supported repository host.") from error


def git_auth_env(token: str | None, host: str = GITHUB_HOST, user: str = "x-access-token") -> dict[str, str]:
    """Build the environment that authenticates git and Git LFS against ``host``.

    Args:
        token: Host token; ``None`` for a public or local remote.
        host: Origin the header is scoped to, so other remotes never see it.
        user: Basic-auth user name the host expects with a token.

    Returns:
        Environment entries to add to every git command.
    """
    environment = {"GIT_TERMINAL_PROMPT": "0", "GIT_LFS_SKIP_SMUDGE": "1"}
    if token:
        basic = base64.b64encode(f"{user}:{token}".encode()).decode()
        environment.update(
            {
                "GIT_CONFIG_COUNT": "1",
                "GIT_CONFIG_KEY_0": f"http.{host}/.extraheader",
                "GIT_CONFIG_VALUE_0": f"AUTHORIZATION: basic {basic}",
            }
        )
    return environment


def _git(arguments: list[str], cwd: Path, environment: dict[str, str]) -> str:
    """Run one git command and return its output.

    Args:
        arguments: Arguments after ``git``.
        cwd: Working directory.
        environment: Extra environment, the auth header included.

    Returns:
        Standard output.

    Raises:
        RepoFetchError: When git fails; the message never carries the token.
    """
    try:
        result = subprocess.run(
            ["git", *arguments],
            cwd=cwd,
            env={**os.environ, **environment},
            capture_output=True,
            text=True,
            timeout=_GIT_TIMEOUT_SECONDS,
            check=False,
        )
    except subprocess.TimeoutExpired as error:
        raise RepoFetchError(f"git {arguments[0]} ran out of time.") from error
    if result.returncode != 0:
        detail = (result.stderr or result.stdout).strip().splitlines()
        raise RepoFetchError(f"git {arguments[0]} failed: {detail[-1] if detail else 'no output'}")
    return result.stdout


def _uses_lfs(root: Path) -> bool:
    """Report whether any ``.gitattributes`` in the tree routes files through Git LFS.

    Args:
        root: Checkout root.

    Returns:
        Whether LFS content has to be fetched.
    """
    for attributes in root.rglob(".gitattributes"):
        if ".git" in attributes.relative_to(root).parts[:-1]:
            continue
        if "filter=lfs" in attributes.read_text(encoding="utf-8", errors="replace"):
            return True
    return False


def _readonly_paths(root: Path, environment: dict[str, str], lfs: bool) -> tuple[str, ...]:
    """List the submodules and Git LFS files an agent may not change.

    Args:
        root: Checkout root with submodules initialized.
        environment: Git environment.
        lfs: Whether the repository uses Git LFS.

    Returns:
        Repository-relative paths, sorted.
    """
    submodules = _git(
        ["submodule", "foreach", "--recursive", "--quiet", 'echo "$displaypath"'], root, environment
    ).splitlines()
    lfs_files = _git(["lfs", "ls-files", "--name-only"], root, environment).splitlines() if lfs else []
    return tuple(sorted({path.strip() for path in [*submodules, *lfs_files] if path.strip()}))


def _without_git(member: tarfile.TarInfo) -> tarfile.TarInfo | None:
    """Drop git metadata so the tree ships as plain files.

    Args:
        member: Entry about to be archived.

    Returns:
        The entry, or ``None`` for ``.git`` folders and submodule ``.git`` files.
    """
    if ".git" in Path(member.name).parts:
        return None
    member.uid = member.gid = 0
    member.uname = member.gname = ""
    return member


def fetch_snapshot(
    repository: str,
    commit: str,
    token: str | None,
    workdir: Path,
    *,
    remote: str | None = None,
    provider: str = "github",
) -> RepoSnapshot:
    """Clone ``repository`` at ``commit`` with submodules and LFS content, then pack the tree.

    Args:
        repository: ``owner/name``.
        commit: The pinned 40-character commit id.
        token: Host token; ``None`` for a public or local remote.
        workdir: Empty parent-owned folder the clone and archive go in.
        remote: Clone URL override, used by tests with a local repository.
        provider: Repository host, see :data:`GIT_HOSTS`.

    Returns:
        The packed snapshot.

    Raises:
        RepoFetchError: When the clone fails, LFS content cannot be fetched,
            or the packed tree is larger than the limit.
    """
    origin, user = git_host(provider)
    environment = git_auth_env(token, origin, user)
    checkout = workdir / "checkout"
    checkout.mkdir(parents=True)
    _git(["init", "--quiet"], checkout, environment)
    _git(["remote", "add", "origin", remote or f"{origin}/{repository}.git"], checkout, environment)
    _git(["fetch", "--quiet", "--depth", "1", "origin", commit], checkout, environment)
    _git(["-c", "advice.detachedHead=false", "checkout", "--quiet", "FETCH_HEAD"], checkout, environment)
    _git(["-c", "protocol.file.allow=always", "submodule", "update", "--init", "--recursive"], checkout, environment)
    lfs = _uses_lfs(checkout)
    if lfs:
        if shutil.which("git-lfs") is None:
            raise RepoFetchError("The repository uses Git LFS, which this server cannot fetch.")
        _git(["lfs", "pull"], checkout, environment)
    readonly = _readonly_paths(checkout, environment, lfs)
    archive = workdir / "tree.tgz"
    with tarfile.open(archive, "w:gz") as packed:
        for entry in sorted(checkout.iterdir()):
            packed.add(entry, arcname=entry.name, filter=_without_git)
    shutil.rmtree(checkout, ignore_errors=True)
    size = archive.stat().st_size
    if size > MAX_ARCHIVE_BYTES:
        archive.unlink()
        raise RepoFetchError(f"The repository is larger than {MAX_ARCHIVE_BYTES // (1024 * 1024)} MB compressed.")
    return RepoSnapshot(commit=commit, archive=archive, readonly_paths=readonly, size_bytes=size)


def patched_repo_zip(
    repository: str,
    commit: str,
    patch: str,
    token: str | None,
    workdir: Path,
    *,
    remote: str | None = None,
    provider: str = "github",
) -> Path:
    """Clone ``repository`` at ``commit``, apply ``patch`` and zip the resulting tree.

    Submodules and Git LFS content are left out: the zip is the files a run's
    versions can change, not a working checkout.

    Args:
        repository: ``owner/name``.
        commit: The pinned commit id.
        patch: A unified diff against ``commit``; empty for the commit as is.
        token: Host token; ``None`` for a public or local remote.
        workdir: Empty folder the clone and zip go in.
        remote: Clone URL override, used by tests with a local repository.
        provider: Repository host, see :data:`GIT_HOSTS`.

    Returns:
        The zip, its entries under one ``<name>/`` folder.

    Raises:
        RepoFetchError: When the clone fails, the patch does not apply, or the
            zip is larger than the limit.
    """
    origin, user = git_host(provider)
    environment = git_auth_env(token, origin, user)
    checkout = workdir / "checkout"
    checkout.mkdir(parents=True)
    _git(["init", "--quiet"], checkout, environment)
    _git(["remote", "add", "origin", remote or f"{origin}/{repository}.git"], checkout, environment)
    _git(["fetch", "--quiet", "--depth", "1", "origin", commit], checkout, environment)
    _git(["-c", "advice.detachedHead=false", "checkout", "--quiet", "FETCH_HEAD"], checkout, environment)
    if patch.strip():
        patch_file = workdir / "version.patch"
        patch_file.write_text(patch if patch.endswith("\n") else patch + "\n", encoding="utf-8")
        _git(["apply", "--whitespace=nowarn", str(patch_file)], checkout, environment)
    folder = repository.rsplit("/", 1)[-1]
    archive = workdir / f"{folder}.zip"
    with zipfile.ZipFile(archive, "w", zipfile.ZIP_DEFLATED) as packed:
        for path in sorted(checkout.rglob("*")):
            relative = path.relative_to(checkout)
            if ".git" in relative.parts or path.is_symlink() or not path.is_file():
                continue
            packed.write(path, f"{folder}/{relative.as_posix()}")
    shutil.rmtree(checkout, ignore_errors=True)
    if archive.stat().st_size > MAX_ARCHIVE_BYTES:
        archive.unlink()
        raise RepoFetchError(f"The repository is larger than {MAX_ARCHIVE_BYTES // (1024 * 1024)} MB compressed.")
    return archive
