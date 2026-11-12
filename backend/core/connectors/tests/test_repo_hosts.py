"""Tests for the provider-to-host dispatch of repository runs."""

from __future__ import annotations

from typing import Any

import pytest

from ...api.errors import DomainError
from .. import bitbucket, github, gitlab, repo_hosts
from ..github_publish import PublishedChange
from ..github_repo import RepoFetchError


def test_host_maps_each_slug_and_defaults_to_github() -> None:
    """Each slug returns its module; a missing slug is GitHub."""
    assert repo_hosts.host("github") is github
    assert repo_hosts.host("gitlab") is gitlab
    assert repo_hosts.host("bitbucket") is bitbucket
    assert repo_hosts.host(None) is github
    with pytest.raises(RepoFetchError, match="not a supported repository host"):
        repo_hosts.host("azure")


def test_provider_of_reads_old_and_new_repo_blocks() -> None:
    """A block saved before other hosts existed is GitHub."""
    assert repo_hosts.provider_of({"full_name": "a/b"}) == "github"
    assert repo_hosts.provider_of({"full_name": "a/b", "provider": "gitlab"}) == "gitlab"


def test_git_kwargs_leave_github_calls_unchanged() -> None:
    """GitHub gets no keyword; other hosts name themselves."""
    assert repo_hosts.git_kwargs("github") == {}
    assert repo_hosts.git_kwargs("bitbucket") == {"provider": "bitbucket"}


def test_host_token_uses_the_host_oauth_app(monkeypatch: pytest.MonkeyPatch) -> None:
    """The token comes from the host's OAuth app and the user's vault entry."""
    calls: list[tuple[str, str]] = []

    def fake_token(app: Any, _vault: Any, username: str) -> str:
        """Record the app and user, and return a token."""
        calls.append((app.provider, username))
        return "tok"

    monkeypatch.setattr(repo_hosts, "current_oauth_token", fake_token)
    assert repo_hosts.host_token(object(), "alice", "gitlab") == "tok"
    assert calls == [("gitlab", "alice")]


def test_host_token_missing_connection_is_a_fetch_error(monkeypatch: pytest.MonkeyPatch) -> None:
    """A missing or expired connection asks the user to connect the host again."""

    def refuse(*_args: Any) -> str:
        """Fail as the vault does for a missing connection."""
        raise DomainError("connectors.not_connected", status=404)

    monkeypatch.setattr(repo_hosts, "current_oauth_token", refuse)
    with pytest.raises(RepoFetchError, match="Connect Bitbucket again"):
        repo_hosts.host_token(object(), "alice", "bitbucket")


def test_resolve_commit_dispatches_by_host(monkeypatch: pytest.MonkeyPatch) -> None:
    """GitHub keeps its own resolver; the other hosts use their module."""
    monkeypatch.setattr(repo_hosts, "github_resolve_commit", lambda *a: f"gh:{a}")
    monkeypatch.setattr(gitlab, "resolve_commit", lambda *a: f"gl:{a}")
    assert repo_hosts.resolve_commit(None, "t", "a/b", "main") == "gh:('t', 'a/b', 'main')"
    assert repo_hosts.resolve_commit("gitlab", "t", "a/b", None) == "gl:('t', 'a/b', None)"


def test_open_change_dispatches_by_host(monkeypatch: pytest.MonkeyPatch) -> None:
    """GitHub opens a pull request through its publisher; Bitbucket through its module."""
    opened: list[str] = []

    def fake(name: str) -> Any:
        """Build a stub opener that records ``name``."""

        def open_it(_token: str, _repo: str, **kwargs: Any) -> PublishedChange:
            """Record the call and return a change."""
            opened.append(name)
            return PublishedChange(branch=kwargs["head"], url="u", number=1, draft=True)

        return open_it

    monkeypatch.setattr(repo_hosts, "open_pull_request", fake("github"))
    monkeypatch.setattr(bitbucket, "open_change", fake("bitbucket"))
    for provider in ("github", "bitbucket"):
        change = repo_hosts.open_change(provider, "t", "a/b", head="h", base_branch=None, title="T", body="B")
        assert change.branch == "h"
    assert opened == ["github", "bitbucket"]
