"""Push a run's best repository version to a new branch and open a draft pull request.

Runs only in the trusted parent, after the run ends. The version is applied to
a fresh clone of the pinned commit, so the branch holds exactly one commit on
top of the code the run started from.

Each call is safe to repeat (SKY-712). A branch that already holds the same
content is not pushed again, and an open pull request for the same head is
returned, not opened again. With ``effects``, each call goes through the
effect log keyed by the step id, so a replayed step returns the stored result
and does not call GitHub.
"""

from __future__ import annotations

import re
import shutil
import subprocess
import tempfile
from collections.abc import Iterable, Mapping, Sequence
from dataclasses import asdict, dataclass
from pathlib import Path
from typing import Any

from ..api.errors import DomainError
from ..brand import APP_NAME
from ..storage.effect_store import StepEffects
from .github import API_URL, PROVIDER, _headers
from .github_repo import RepoFetchError, _git, git_auth_env, git_host
from .repo_drafts import DraftFileError, write_to_checkout
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


class BranchConflictError(RepoPublishError):
    """The branch already holds other content, so a push would replace it."""


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

    def __post_init__(self) -> None:
        """Refuse counts that are not whole numbers of zero or more, or more fixes than attacks.

        Raises:
            ValueError: A count is a bool, not an int, or negative, or
                ``exploits_fixed`` is larger than ``attacks``.
        """
        for count in (self.attacks, self.exploits_fixed):
            if isinstance(count, bool) or not isinstance(count, int) or count < 0:
                raise ValueError(f"Hardening counts must be whole numbers of zero or more, not {count!r}.")
        if self.exploits_fixed > self.attacks:
            raise ValueError("A hardening job cannot fix more exploits than it ran attacks.")


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
    provider: str = "github",
    drafts: Mapping[str, str] | None = None,
    readonly_paths: Iterable[str] = (),
    effects: StepEffects | None = None,
) -> str:
    """Commit ``patch`` on top of ``commit`` and push it as a new branch.

    The run's draft files go into the same commit, under the patch, because
    the version was made against the tree with them. When the branch already
    holds the same tree, nothing is pushed.

    Args:
        repository: ``owner/name``.
        commit: The pinned commit the version was made against.
        patch: ``git diff --binary`` output of the version.
        branch: New branch name.
        message: Commit message.
        token: Host token; ``None`` for a local remote.
        remote: Push URL override, used by tests with a local repository.
        provider: Repository host, see :data:`~.github_repo.GIT_HOSTS`.
        drafts: The run's ``draft_files``; ``None`` for none.
        readonly_paths: The run's submodule and Git LFS paths, which a draft cannot change.
        effects: The effect log of the step that publishes; ``None`` pushes with no log.

    Returns:
        The commit id on the branch.

    Raises:
        BranchConflictError: When the branch already holds other content.
        RepoPublishError: When the clone, a draft file, the patch or the push fails.
    """
    if effects is not None:
        return effects.once(
            "push_version",
            lambda: {
                "commit": push_version(
                    repository,
                    commit,
                    patch,
                    branch,
                    message,
                    token,
                    remote=remote,
                    provider=provider,
                    drafts=drafts,
                    readonly_paths=readonly_paths,
                )
            },
        )["commit"]
    workdir = Path(tempfile.mkdtemp(prefix="app-publish-"))
    try:
        origin, user = git_host(provider)
        environment = git_auth_env(token, origin, user)
        _git(["init", "--quiet"], workdir, environment)
        _git(["remote", "add", "origin", remote or f"{origin}/{repository}.git"], workdir, environment)
        _git(["fetch", "--quiet", "--depth", "1", "origin", commit], workdir, environment)
        _git([*_NO_LFS, "-c", "advice.detachedHead=false", "checkout", "--quiet", "FETCH_HEAD"], workdir, environment)
        _stage_drafts(workdir, drafts, readonly_paths, environment)
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
        head = _git(["rev-parse", "HEAD"], workdir, environment).strip()
        existing = _existing_branch(workdir, branch, head, environment)
        if existing is not None:
            return existing
        _git(["push", "--quiet", "origin", f"HEAD:refs/heads/{branch}"], workdir, environment)
        return head
    except RepoFetchError as error:
        raise RepoPublishError(f"Could not push the branch: {error}") from error
    finally:
        shutil.rmtree(workdir, ignore_errors=True)


def _existing_branch(workdir: Path, branch: str, commit: str, environment: dict[str, str]) -> str | None:
    """Return the remote branch's commit when it holds the same tree as ``commit``.

    Args:
        workdir: The local repository with ``origin`` and ``commit``.
        branch: The branch name.
        commit: The local commit to compare with.
        environment: The git environment.

    Returns:
        The remote commit id, or ``None`` when the branch does not exist.

    Raises:
        BranchConflictError: When the branch holds another tree.
    """
    ref = f"refs/heads/{branch}"
    # ls-remote matches a pattern by its tail, so ``refs/heads/x`` also lists ``refs/heads/a/refs/heads/x``.
    lines = _git(["ls-remote", "origin", ref], workdir, environment).splitlines()
    remote = next((line.split()[0] for line in lines if line.split()[1:] == [ref]), None)
    if remote is None:
        return None
    if remote != commit:
        _git(["fetch", "--quiet", "--depth", "1", "origin", ref], workdir, environment)
        remote_tree = _git(["rev-parse", "FETCH_HEAD^{tree}"], workdir, environment).strip()
        if remote_tree != _git(["rev-parse", f"{commit}^{{tree}}"], workdir, environment).strip():
            raise BranchConflictError(f"The branch '{branch}' already holds other content.")
    return remote


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
    provider: str = "github",
    drafts: Mapping[str, str] | None = None,
    readonly_paths: Iterable[str] = (),
    effects: StepEffects | None = None,
) -> dict[str, str]:
    """Push each version to its own branch and move the best branch to one of them.

    Every candidate commit takes its dates from ``commit``, so the same
    version always makes the same commit id. A retry then pushes nothing new,
    and only ``skynet/best`` is ever rewritten. A candidate branch that holds
    the same tree under another commit keeps that commit.

    Args:
        repository: ``owner/name``.
        commit: The pinned commit every version was made against.
        versions: ``(version id, git diff --binary against commit)`` pairs.
            An empty patch puts the branch on ``commit`` itself.
        best_id: Id of the version ``skynet/best`` points at.
        token: Host token; ``None`` for a local remote.
        remote: Push URL override, used by tests with a local repository.
        provider: Repository host, see :data:`~.github_repo.GIT_HOSTS`.
        drafts: The run's ``draft_files``, committed with every version; ``None`` for none.
        readonly_paths: The run's submodule and Git LFS paths, which a draft cannot change.
        effects: The effect log of the step that publishes; ``None`` pushes with no log.

    Returns:
        Each pushed branch, ``skynet/best`` included, mapped to its commit id.

    Raises:
        BranchConflictError: When a candidate branch already holds other content.
        RepoPublishError: When an id is invalid or repeated, ``best_id`` is
            not a version, a draft file or a patch does not apply, or the push fails.
    """
    if effects is not None:
        return effects.once(
            "push_candidates",
            lambda: push_candidates(
                repository,
                commit,
                versions,
                best_id,
                token,
                remote=remote,
                provider=provider,
                drafts=drafts,
                readonly_paths=readonly_paths,
            ),
        )
    branches = [candidate_branch(version_id) for version_id, _ in versions]
    if len(set(branches)) != len(branches):
        raise RepoPublishError("Two candidate versions have the same id.")
    if best_id not in {version_id for version_id, _ in versions}:
        raise RepoPublishError(f"The best version '{best_id}' is not one of the candidates.")
    workdir = Path(tempfile.mkdtemp(prefix="app-candidates-"))
    try:
        origin, user = git_host(provider)
        environment = git_auth_env(token, origin, user)
        _git(["init", "--quiet"], workdir, environment)
        _git(["remote", "add", "origin", remote or f"{origin}/{repository}.git"], workdir, environment)
        _git(["fetch", "--quiet", "--depth", "1", "origin", commit], workdir, environment)
        date = _git(["log", "-1", "--format=%cI", "FETCH_HEAD"], workdir, environment).strip()
        dated = {**environment, "GIT_AUTHOR_DATE": date, "GIT_COMMITTER_DATE": date}
        pushed: dict[str, str] = {}
        for (version_id, patch), branch in zip(versions, branches, strict=True):
            _git(
                [*_NO_LFS, "-c", "advice.detachedHead=false", "checkout", "--quiet", "--force", commit], workdir, dated
            )
            staged = _stage_drafts(workdir, drafts, readonly_paths, dated)
            if patch.strip():
                _apply(workdir, patch, version_id)
            if patch.strip() or staged:
                message = f"{APP_NAME} candidate {version_id}"
                _git([*COMMIT_AUTHOR, *_NO_LFS, "commit", "--quiet", "--no-verify", "-m", message], workdir, dated)
            head = _git(["rev-parse", "HEAD"], workdir, dated).strip()
            pushed[branch] = _existing_branch(workdir, branch, head, dated) or head
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


def _stage_drafts(
    workdir: Path, drafts: Mapping[str, str] | None, readonly_paths: Iterable[str], environment: dict[str, str]
) -> bool:
    """Write the run's draft files into the checkout and add them to the index.

    Args:
        workdir: Checkout of the pinned commit.
        drafts: The run's ``draft_files``; ``None`` for none.
        readonly_paths: The run's submodule and Git LFS paths, which a draft cannot change.
        environment: The git environment.

    Returns:
        Whether the drafts changed the index.

    Raises:
        RepoPublishError: When a draft file cannot be written.
    """
    try:
        paths = write_to_checkout(workdir, drafts, readonly_paths)
    except DraftFileError as error:
        raise RepoPublishError(f"A draft file of the run cannot be written: {error}") from None
    if not paths:
        return False
    # A draft path is a file name, never a pattern: "a*.py" must not also add "ab.py".
    # --force: the user chose each draft, so a path the repo's .gitignore ignores is still published.
    _git([*_NO_LFS, "--literal-pathspecs", "add", "--force", "--", *paths], workdir, environment)
    changed = subprocess.run(["git", "diff", "--cached", "--quiet"], cwd=workdir, capture_output=True, check=False)
    return changed.returncode != 0


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
    effects: StepEffects | None = None,
) -> PublishedChange:
    """Open a draft pull request, or a regular one where the plan has no drafts.

    When an open pull request for ``head`` exists, return it and open none.

    Args:
        token: GitHub token with ``repo`` scope.
        repository: ``owner/name``.
        head: The pushed branch.
        base_branch: Branch to merge into; ``None`` picks the default branch.
        title: Pull request title.
        body: Pull request description.
        hardening: The run's finished hardening job; ``None`` keeps the body as given.
        effects: The effect log of the step that publishes; ``None`` opens with no log.

    Returns:
        The opened or found pull request.

    Raises:
        RepoPublishError: When GitHub refuses the request.
    """
    if effects is not None:
        found = effects.once(
            "open_pull_request",
            lambda: asdict(
                open_pull_request(
                    token,
                    repository,
                    head=head,
                    base_branch=base_branch,
                    title=title,
                    body=body,
                    hardening=hardening,
                )
            ),
        )
        return PublishedChange(**found)
    url = f"{API_URL}/repos/{repository}/pulls"
    owner = repository.split("/", 1)[0]
    try:
        open_ones = get_json(
            url, provider=PROVIDER, headers=_headers(token), params={"head": f"{owner}:{head}", "state": "open"}
        )
    except DomainError as error:
        raise RepoPublishError("GitHub refused to list the open pull requests.") from error
    if open_ones:
        return _published(head, open_ones[0])
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
    return _published(head, response.json())


def _published(head: str, pull: dict[str, Any]) -> PublishedChange:
    """Return the change for one GitHub pull request object.

    Args:
        head: The pushed branch.
        pull: The pull request as GitHub returns it.

    Returns:
        The change.
    """
    return PublishedChange(
        branch=head, url=str(pull["html_url"]), number=int(pull["number"]), draft=bool(pull.get("draft"))
    )
