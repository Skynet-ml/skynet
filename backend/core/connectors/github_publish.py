"""Push a run's best repository version to a new branch and open a draft pull request.

Runs only in the trusted parent, after the run ends. The version is applied to
a fresh clone of the pinned commit, so the branch holds exactly one commit on
top of the code the run started from.
"""

from __future__ import annotations

import re
import shutil
import subprocess
import tempfile
from collections.abc import Sequence
from dataclasses import dataclass
from pathlib import Path

from ..api.errors import DomainError
from ..brand import APP_NAME
from .github import API_URL, PROVIDER, _headers
from .github_repo import GITHUB_HOST, RepoFetchError, _git, git_auth_env
from .transport import get_json, request

COMMIT_AUTHOR = ["-c", f"user.name={APP_NAME}", "-c", "user.email=noreply@skynetml.com", "-c", "commit.gpgsign=false"]
# The branch holds a reviewed diff, never LFS objects, so LFS filters stay off.
_NO_LFS = ["-c", "filter.lfs.process=", "-c", "filter.lfs.clean=cat", "-c", "filter.lfs.smudge=cat"]
CANDIDATE_PREFIX = "skynet/candidate-"
BEST_BRANCH = "skynet/best"
# Stricter than git's ref rules, so an id never needs escaping in a refspec.
_REF_SAFE = re.compile(r"[A-Za-z0-9][A-Za-z0-9_-]{0,99}")


class RepoPublishError(RuntimeError):
    """Explain, in words a run's owner can act on, why the pull request was not opened."""


@dataclass(frozen=True)
class PublishedChange:
    """The branch and pull request holding a run's best version."""

    branch: str
    url: str
    number: int
    draft: bool


@dataclass(frozen=True)
class HardeningSummary:
    """What a finished metric-hardening job did to the run's metric."""

    attacks: int
    exploits_fixed: int


def hardening_line(summary: HardeningSummary) -> str:
    """Write the pull request body line that reports a finished hardening job.

    Args:
        summary: The finished job's attack and fix counts.

    Returns:
        For example ``Metric survived 12 attacks, 3 exploits fixed``.
    """
    attacks = "attack" if summary.attacks == 1 else "attacks"
    exploits = "exploit" if summary.exploits_fixed == 1 else "exploits"
    return f"Metric survived {summary.attacks} {attacks}, {summary.exploits_fixed} {exploits} fixed"


def _base_branch(token: str, repository: str, branch: str | None) -> str:
    """Return the branch the pull request targets.

    Args:
        token: GitHub token.
        repository: ``owner/name``.
        branch: Branch the run started from, if it named one.

    Returns:
        That branch, or the repository's default branch.
    """
    if branch:
        return branch
    repo = get_json(f"{API_URL}/repos/{repository}", provider=PROVIDER, headers=_headers(token))
    return str(repo["default_branch"])


def push_version(
    repository: str,
    commit: str,
    patch: str,
    branch: str,
    message: str,
    token: str | None,
    *,
    remote: str | None = None,
) -> None:
    """Commit ``patch`` on top of ``commit`` and push it as a new branch.

    Args:
        repository: ``owner/name``.
        commit: The pinned commit the version was made against.
        patch: ``git diff --binary`` output of the version.
        branch: New branch name.
        message: Commit message.
        token: GitHub token; ``None`` for a local remote.
        remote: Push URL override, used by tests with a local repository.

    Raises:
        RepoPublishError: When the clone, the patch or the push fails.
    """
    environment = git_auth_env(token)
    workdir = Path(tempfile.mkdtemp(prefix="app-publish-"))
    try:
        _git(["init", "--quiet"], workdir, environment)
        _git(["remote", "add", "origin", remote or f"{GITHUB_HOST}/{repository}.git"], workdir, environment)
        _git(["fetch", "--quiet", "--depth", "1", "origin", commit], workdir, environment)
        _git([*_NO_LFS, "-c", "advice.detachedHead=false", "checkout", "--quiet", "FETCH_HEAD"], workdir, environment)
        applied = subprocess.run(
            ["git", "apply", "--index", "--binary", "--whitespace=nowarn", "-"],
            cwd=workdir,
            input=patch,
            capture_output=True,
            text=True,
            check=False,
        )
        if applied.returncode != 0:
            raise RepoPublishError("The best version no longer applies to the commit the run started from.")
        _git([*COMMIT_AUTHOR, *_NO_LFS, "commit", "--quiet", "--no-verify", "-m", message], workdir, environment)
        _git(["push", "--quiet", "origin", f"HEAD:refs/heads/{branch}"], workdir, environment)
    except RepoFetchError as error:
        raise RepoPublishError(f"Could not push the branch: {error}") from error
    finally:
        shutil.rmtree(workdir, ignore_errors=True)


def candidate_branch(version_id: str) -> str:
    """Return the branch that holds one candidate version.

    Args:
        version_id: The version's id.

    Returns:
        ``skynet/candidate-<id>``.

    Raises:
        RepoPublishError: When the id cannot be part of a branch name.
    """
    if not _REF_SAFE.fullmatch(version_id):
        raise RepoPublishError(f"'{version_id}' cannot be used in a branch name.")
    return f"{CANDIDATE_PREFIX}{version_id}"


def push_candidates(
    repository: str,
    commit: str,
    versions: Sequence[tuple[str, str]],
    best_id: str,
    token: str | None,
    *,
    remote: str | None = None,
) -> dict[str, str]:
    """Push each version to its own branch and move the best branch to one of them.

    Every candidate commit takes its dates from ``commit``, so the same
    version always makes the same commit id. A retry then pushes nothing new,
    and only ``skynet/best`` is ever rewritten.

    Args:
        repository: ``owner/name``.
        commit: The pinned commit every version was made against.
        versions: ``(version id, git diff --binary against commit)`` pairs.
            An empty patch puts the branch on ``commit`` itself.
        best_id: Id of the version ``skynet/best`` points at.
        token: GitHub token; ``None`` for a local remote.
        remote: Push URL override, used by tests with a local repository.

    Returns:
        Each pushed branch, ``skynet/best`` included, mapped to its commit id.

    Raises:
        RepoPublishError: When an id is invalid or repeated, ``best_id`` is
            not a version, a patch does not apply, or the push fails.
    """
    branches = [candidate_branch(version_id) for version_id, _ in versions]
    if len(set(branches)) != len(branches):
        raise RepoPublishError("Two candidate versions have the same id.")
    if best_id not in {version_id for version_id, _ in versions}:
        raise RepoPublishError(f"The best version '{best_id}' is not one of the candidates.")
    environment = git_auth_env(token)
    workdir = Path(tempfile.mkdtemp(prefix="app-candidates-"))
    try:
        _git(["init", "--quiet"], workdir, environment)
        _git(["remote", "add", "origin", remote or f"{GITHUB_HOST}/{repository}.git"], workdir, environment)
        _git(["fetch", "--quiet", "--depth", "1", "origin", commit], workdir, environment)
        date = _git(["log", "-1", "--format=%cI", "FETCH_HEAD"], workdir, environment).strip()
        dated = {**environment, "GIT_AUTHOR_DATE": date, "GIT_COMMITTER_DATE": date}
        pushed: dict[str, str] = {}
        for (version_id, patch), branch in zip(versions, branches, strict=True):
            _git(
                [*_NO_LFS, "-c", "advice.detachedHead=false", "checkout", "--quiet", "--force", commit], workdir, dated
            )
            if patch.strip():
                _apply(workdir, patch, version_id)
                message = f"{APP_NAME} candidate {version_id}"
                _git([*COMMIT_AUTHOR, *_NO_LFS, "commit", "--quiet", "--no-verify", "-m", message], workdir, dated)
            pushed[branch] = _git(["rev-parse", "HEAD"], workdir, dated).strip()
        pushed[BEST_BRANCH] = pushed[candidate_branch(best_id)]
        refspecs = [f"{sha}:refs/heads/{branch}" for branch, sha in pushed.items() if branch != BEST_BRANCH]
        _git(
            ["push", "--quiet", "--atomic", "origin", *refspecs, f"+{pushed[BEST_BRANCH]}:refs/heads/{BEST_BRANCH}"],
            workdir,
            dated,
        )
        return pushed
    except RepoFetchError as error:
        raise RepoPublishError(f"Could not push the candidate branches: {error}") from error
    finally:
        shutil.rmtree(workdir, ignore_errors=True)


def _apply(workdir: Path, patch: str, version_id: str) -> None:
    """Apply one version's patch to the index and working tree.

    Args:
        workdir: Checkout of the pinned commit.
        patch: ``git diff --binary`` output.
        version_id: Version id, for the error message.

    Raises:
        RepoPublishError: When the patch does not apply.
    """
    applied = subprocess.run(
        ["git", "apply", "--index", "--binary", "--whitespace=nowarn", "-"],
        cwd=workdir,
        input=patch if patch.endswith("\n") else patch + "\n",
        capture_output=True,
        text=True,
        check=False,
    )
    if applied.returncode != 0:
        raise RepoPublishError(f"Version {version_id} does not apply to the commit the run started from.")


def open_pull_request(
    token: str,
    repository: str,
    *,
    head: str,
    base_branch: str | None,
    title: str,
    body: str,
    hardening: HardeningSummary | None = None,
) -> PublishedChange:
    """Open a draft pull request, or a regular one where the plan has no drafts.

    Args:
        token: GitHub token with ``repo`` scope.
        repository: ``owner/name``.
        head: The pushed branch.
        base_branch: Branch to merge into; ``None`` picks the default branch.
        title: Pull request title.
        body: Pull request description.
        hardening: The run's finished hardening job; ``None`` keeps the body as given.

    Returns:
        The opened pull request.

    Raises:
        RepoPublishError: When GitHub refuses the request.
    """
    url = f"{API_URL}/repos/{repository}/pulls"
    if hardening is not None:
        body = f"{body}\n\n{hardening_line(hardening)}" if body else hardening_line(hardening)
    try:
        fields = {"title": title, "head": head, "base": _base_branch(token, repository, base_branch), "body": body}
        response = request("POST", url, provider=PROVIDER, headers=_headers(token), json={**fields, "draft": True})
    except DomainError as error:
        # Private repositories on free plans refuse drafts with a 422.
        if error.params.get("status_code") != 422:
            raise RepoPublishError("GitHub refused to open the pull request.") from error
        try:
            response = request("POST", url, provider=PROVIDER, headers=_headers(token), json=fields)
        except DomainError as retry_error:
            raise RepoPublishError("GitHub refused to open the pull request.") from retry_error
    opened = response.json()
    return PublishedChange(
        branch=head, url=str(opened["html_url"]), number=int(opened["number"]), draft=bool(opened.get("draft"))
    )
