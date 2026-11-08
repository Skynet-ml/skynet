"""Pick the GitHub, GitLab or Bitbucket module for a repository run.

Callers that read, clone or publish a repository hold a provider slug next
to the ``owner/name`` path. This module maps the slug to the host's module
and gives the three calls whose names differ between hosts: the token
lookup, the commit pin and the opening of the pull or merge request.
"""

from __future__ import annotations

from types import ModuleType
from typing import Any, Literal

from ..api.errors import DomainError
from . import bitbucket, github, gitlab
from .github_publish import HardeningSummary, PublishedChange, open_pull_request
from .github_repo import RepoFetchError
from .github_repo import resolve_commit as github_resolve_commit
from .oauth import current_oauth_token
from .transport import label
from .vault import ConnectorVault

RepoProvider = Literal["github", "gitlab", "bitbucket"]
HOSTS: dict[str, ModuleType] = {github.PROVIDER: github, gitlab.PROVIDER: gitlab, bitbucket.PROVIDER: bitbucket}
DEFAULT_PROVIDER = github.PROVIDER


def host(provider: str | None) -> ModuleType:
    """Return the connector module of a repository host.

    Args:
        provider: ``github``, ``gitlab`` or ``bitbucket``; ``None`` means GitHub,
            for runs saved before other hosts existed.

    Returns:
        The module.

    Raises:
        RepoFetchError: For any other slug.
    """
    module = HOSTS.get(provider or DEFAULT_PROVIDER)
    if module is None:
        raise RepoFetchError(f"'{provider}' is not a supported repository host.")
    return module


def provider_of(repo: dict[str, Any]) -> str:
    """Return the host slug of a stored ``target.repo`` block.

    Args:
        repo: The run's repository block.

    Returns:
        Its ``provider``, or ``github`` for blocks saved before other hosts existed.

    Raises:
        RepoFetchError: For a slug that is not a repository host.
    """
    return host(repo.get("provider")).PROVIDER


def git_kwargs(provider: str) -> dict[str, str]:
    """Return the keyword that points the git clone and push helpers at a host.

    GitHub calls get no keyword, so they keep the exact call they had before
    other hosts existed.

    Args:
        provider: The repository host.

    Returns:
        ``{}`` for GitHub, else ``{"provider": provider}``.
    """
    return {} if provider == DEFAULT_PROVIDER else {"provider": provider}


def host_token(engine: Any, username: str, provider: str | None) -> str:
    """Return a usable token from the user's connection to a repository host.

    Args:
        engine: SQLAlchemy engine holding the connector vault.
        username: Owner of the connection.
        provider: The repository host.

    Returns:
        The bearer token, refreshed when an OAuth token went stale.

    Raises:
        RepoFetchError: When the host is not connected or the link expired.
    """
    module = host(provider)
    try:
        return current_oauth_token(module.oauth_app(), ConnectorVault(engine), username)
    except DomainError as error:
        raise RepoFetchError(
            f"Connect {label(module.PROVIDER)} again: the connection is missing or has expired."
        ) from error


def resolve_commit(provider: str | None, token: str, repository: str, branch: str | None) -> str:
    """Pin a branch, or the default branch, to its current commit on a host.

    Args:
        provider: The repository host.
        token: Host token with read access to the repository.
        repository: The repository path.
        branch: Branch to pin; ``None`` picks the default branch.

    Returns:
        The 40-character commit id.

    Raises:
        RepoFetchError: When the host refuses the lookup.
    """
    module = host(provider)
    if module is github:
        return github_resolve_commit(token, repository, branch)
    return module.resolve_commit(token, repository, branch)


def open_change(
    provider: str | None,
    token: str,
    repository: str,
    *,
    head: str,
    base_branch: str | None,
    title: str,
    body: str,
    hardening: HardeningSummary | None = None,
) -> PublishedChange:
    """Open a draft pull request (GitHub, Bitbucket) or merge request (GitLab).

    Args:
        provider: The repository host.
        token: Host token with write access.
        repository: The repository path.
        head: The pushed branch.
        base_branch: Branch to merge into; ``None`` picks the default branch.
        title: Title of the change.
        body: Description of the change.
        hardening: The run's finished hardening job, if any.

    Returns:
        The opened change.

    Raises:
        RepoPublishError: When the host refuses the request.
    """
    module = host(provider)
    if module is github:
        return open_pull_request(
            token, repository, head=head, base_branch=base_branch, title=title, body=body, hardening=hardening
        )
    return module.open_change(
        token, repository, head=head, base_branch=base_branch, title=title, body=body, hardening=hardening
    )
