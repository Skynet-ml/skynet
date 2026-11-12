"""Create a new GitHub repository in the user's account and push its first commit.

Runs only in the trusted parent, with the token of the user's GitHub
connection. The ``repo`` scope that connection already asks for allows
``POST /user/repos`` and pushes, so no new OAuth permission is needed.
"""

from __future__ import annotations

import shutil
import tempfile
from dataclasses import dataclass
from pathlib import Path, PurePosixPath

from ..api.errors import DomainError
from .github import API_URL, PROVIDER, _headers
from .github_publish import _NO_LFS, COMMIT_AUTHOR
from .github_repo import GITHUB_HOST, RepoFetchError, _git, git_auth_env
from .transport import get_json, request


class RepoCreateError(RuntimeError):
    """Explain, in words the user can act on, why the repository was not created."""


@dataclass(frozen=True)
class CreatedRepository:
    """A new repository and the first commit on its default branch."""

    full_name: str
    default_branch: str
    commit: str


def _name_conflict(token: str, name: str) -> bool:
    """Report whether the user's account already holds a repository called ``name``.

    Args:
        token: GitHub token.
        name: Repository name GitHub refused.

    Returns:
        Whether the name is taken.
    """
    return existing_repository(token, name) is not None


def existing_repository(token: str, name: str) -> tuple[str, str] | None:
    """Find a repository called ``name`` in the user's account.

    Args:
        token: GitHub token.
        name: Repository name.

    Returns:
        The ``owner/name`` and default branch, or ``None`` when no such repository exists.

    Raises:
        RepoCreateError: When GitHub fails with anything but 404, so an
            outage never looks like a free name.
    """
    login = str(get_json(f"{API_URL}/user", provider=PROVIDER, headers=_headers(token))["login"])
    try:
        found = get_json(f"{API_URL}/repos/{login}/{name}", provider=PROVIDER, headers=_headers(token))
    except DomainError as error:
        if error.code == "connectors.not_found":
            return None
        raise RepoCreateError(f"Could not check GitHub for a repository named '{name}'. Retry later.") from error
    return str(found["full_name"]), str(found.get("default_branch") or "main")


def create_repository(token: str, name: str, *, private: bool, description: str | None = None) -> tuple[str, str]:
    """Create an empty repository in the user's GitHub account.

    Args:
        token: GitHub token with ``repo`` scope.
        name: Repository name.
        private: Whether only the user can see the repository.
        description: Optional one-line description.

    Returns:
        The ``owner/name`` and the default branch GitHub chose.

    Raises:
        RepoCreateError: When the name is taken or GitHub refuses the request.
    """
    fields: dict[str, object] = {"name": name, "private": private, "auto_init": False}
    if description:
        fields["description"] = description
    try:
        response = request("POST", f"{API_URL}/user/repos", provider=PROVIDER, headers=_headers(token), json=fields)
    except DomainError as error:
        # GitHub answers 422 both for a taken name and for an invalid one.
        if error.params.get("status_code") == 422:
            if _name_conflict(token, name):
                raise RepoCreateError(
                    f"Your GitHub account already has a repository named '{name}'. Choose another name."
                ) from error
            raise RepoCreateError(f"GitHub does not accept '{name}' as a repository name.") from error
        raise RepoCreateError("GitHub refused to create the repository. Connect GitHub again and retry.") from error
    created = response.json()
    return str(created["full_name"]), str(created.get("default_branch") or "main")


def _safe_path(raw: str) -> PurePosixPath:
    """Check that a file path stays inside the repository.

    Args:
        raw: Repository-relative path.

    Returns:
        The path.

    Raises:
        RepoCreateError: For an empty, absolute, ``..`` or ``.git`` path.
    """
    path = PurePosixPath(raw)
    if not raw or path.is_absolute() or any(part in ("", ".", "..", ".git") for part in path.parts):
        raise RepoCreateError(f"'{raw}' is not a valid file path for the new repository.")
    return path


def _checked_paths(files: dict[str, str]) -> dict[str, PurePosixPath]:
    """Check every path of the first commit before anything is created.

    Args:
        files: Repository-relative path to text content.

    Returns:
        Each raw path mapped to its parsed form.

    Raises:
        RepoCreateError: When ``files`` is empty or a path is not valid.
    """
    if not files:
        raise RepoCreateError("The new repository needs at least one file.")
    return {raw: _safe_path(raw) for raw in files}


def push_initial_commit(
    repository: str,
    branch: str,
    files: dict[str, str],
    message: str,
    token: str | None,
    *,
    remote: str | None = None,
) -> str:
    """Push ``files`` as the first commit of ``branch`` in an empty repository.

    A retry after a lost response is safe: when the branch already holds a
    commit with exactly these files, that commit is returned and nothing is pushed.

    Args:
        repository: ``owner/name``.
        branch: Branch to create, normally the default branch.
        files: Repository-relative path to text content.
        message: Commit message.
        token: GitHub token; ``None`` for a local remote.
        remote: Push URL override, used by tests with a local repository.

    Returns:
        The 40-character commit id on ``branch``.

    Raises:
        RepoCreateError: When ``files`` is empty or has a bad path, the branch
            already holds other content, or git fails.
    """
    paths = _checked_paths(files)
    environment = git_auth_env(token)
    workdir = Path(tempfile.mkdtemp(prefix="app-create-"))
    try:
        _git(["init", "--quiet"], workdir, environment)
        _git(["remote", "add", "origin", remote or f"{GITHUB_HOST}/{repository}.git"], workdir, environment)
        for raw, path in paths.items():
            target = workdir.joinpath(*path.parts)
            target.parent.mkdir(parents=True, exist_ok=True)
            target.write_text(files[raw], encoding="utf-8")
        _git([*_NO_LFS, "add", "--all"], workdir, environment)
        _git([*COMMIT_AUTHOR, *_NO_LFS, "commit", "--quiet", "--no-verify", "-m", message], workdir, environment)
        ref = f"refs/heads/{branch}"
        existing = _git(["ls-remote", "origin", ref], workdir, environment).split()
        if existing:
            _git(["fetch", "--quiet", "--depth", "1", "origin", ref], workdir, environment)
            remote_tree = _git(["rev-parse", "FETCH_HEAD^{tree}"], workdir, environment).strip()
            if remote_tree != _git(["rev-parse", "HEAD^{tree}"], workdir, environment).strip():
                raise RepoCreateError(f"{repository} already has other content on '{branch}'.")
            return existing[0]
        _git(["push", "--quiet", "origin", f"HEAD:{ref}"], workdir, environment)
        return _git(["rev-parse", "HEAD"], workdir, environment).strip()
    except RepoFetchError as error:
        raise RepoCreateError(f"Could not push the first commit: {error}") from error
    finally:
        shutil.rmtree(workdir, ignore_errors=True)


def create_repository_with_files(
    token: str,
    name: str,
    files: dict[str, str],
    *,
    private: bool,
    description: str | None = None,
    message: str = "Initial commit",
    remote: str | None = None,
) -> CreatedRepository:
    """Create a repository in the user's account and push ``files`` as its first commit.

    If the push fails after GitHub created the repository, retry with
    :func:`push_initial_commit`: a second create would hit the name conflict.

    Args:
        token: GitHub token with ``repo`` scope.
        name: Repository name.
        files: Repository-relative path to text content.
        private: Whether only the user can see the repository.
        description: Optional one-line description.
        message: First commit message.
        remote: Push URL override, used by tests with a local repository.

    Returns:
        The repository, its default branch and the pinned first commit.

    Raises:
        RepoCreateError: When creation or the first push fails.
    """
    _checked_paths(files)
    full_name, branch = create_repository(token, name, private=private, description=description)
    commit = push_initial_commit(full_name, branch, files, message, token, remote=remote)
    return CreatedRepository(full_name=full_name, default_branch=branch, commit=commit)
