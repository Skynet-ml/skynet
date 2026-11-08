"""Route tests for the GitLab and Bitbucket repository picker, against stubbed host APIs."""

from __future__ import annotations

import base64
from datetime import UTC, datetime, timedelta
from typing import Any
from unittest.mock import patch
from urllib.parse import unquote

import httpx
import pytest

from ...config import settings
from ...connectors.vault import ConnectorVault
from .test_connectors_router import (
    _make_client,
    _response,
    vault_key,  # noqa: F401 - fixture re-export
)

SHA = "c" * 40


def _gitlab_api(method: str, url: str, **kwargs: Any) -> httpx.Response:
    """Answer the gitlab.com calls the picker routes make.

    Args:
        method: HTTP method.
        url: Full request URL.
        **kwargs: The request's keyword arguments.

    Returns:
        A canned response.

    Raises:
        AssertionError: For a call the test does not expect.
    """
    path = unquote(url.removeprefix("https://gitlab.com/api/v4"))
    if path == "/user":
        return _response(200, {"username": "alice"})
    if path == "/projects":
        project = {"path_with_namespace": "team/sub/bot", "visibility": "private", "default_branch": "main"}
        return _response(200, [project])
    if path == "/projects/team/sub/bot":
        return _response(200, {"default_branch": "main"})
    if path == "/projects/team/sub/bot/repository/branches":
        return _response(200, [{"name": "main"}, {"name": "dev"}] if kwargs["params"]["page"] == 1 else [])
    if path == "/projects/team/sub/bot/repository/commits":
        return _response(200, [{"id": SHA, "message": "First\nrest", "committed_date": "2026-11-01T00:00:00Z"}])
    if path == "/projects/team/sub/bot/repository/tree":
        return _response(200, [{"path": "app.py", "type": "blob"}])
    if path == "/projects/team/sub/bot/repository/files/app.py":
        encoded = base64.b64encode(b"print(1)\n").decode()
        return _response(200, {"content": encoded, "encoding": "base64", "size": 9})
    raise AssertionError(f"unexpected call {method} {url}")


def _bitbucket_api(method: str, url: str, **kwargs: Any) -> httpx.Response:
    """Answer the Bitbucket Cloud calls the picker routes make.

    Args:
        method: HTTP method.
        url: Full request URL.
        **kwargs: The request's keyword arguments.

    Returns:
        A canned response.

    Raises:
        AssertionError: For a call the test does not expect.
    """
    path = url.removeprefix("https://api.bitbucket.org/2.0")
    if path == "/user":
        return _response(200, {"username": "alice"})
    if path == "/repositories":
        return _response(200, {"values": [{"full_name": "team/bot", "is_private": True}]})
    if path == "/repositories/team/bot":
        return _response(200, {"full_name": "team/bot", "mainbranch": {"name": "main"}})
    if path == "/repositories/team/bot/refs/branches":
        return _response(200, {"values": [{"name": "main"}]})
    if path == "/repositories/team/bot/commit/main":
        return _response(200, {"hash": SHA})
    if path == f"/repositories/team/bot/src/{SHA}/":
        return _response(200, {"values": [{"path": "app.py", "type": "commit_file", "size": 3}]})
    raise AssertionError(f"unexpected call {method} {url}")


@pytest.mark.usefixtures("vault_key")
def test_gitlab_picker_routes_list_and_read() -> None:
    """A linked GitLab account lists projects, branches, the tree and one file."""
    client, _ = _make_client()
    with patch("core.connectors.transport.CLIENT.request", side_effect=_gitlab_api):
        linked = client.put("/connectors/gitlab/credentials", json={"fields": {"token": "glpat-x"}})
        repos = client.get("/connectors/gitlab/repos")
        branches = client.get("/connectors/gitlab/branches", params={"repo": "team/sub/bot"})
        tree = client.get("/connectors/gitlab/tree", params={"repo": "team/sub/bot"})
        commits = client.get("/connectors/gitlab/commits", params={"repo": "team/sub/bot", "branch": "dev"})
        file = client.get("/connectors/gitlab/file", params={"repo": "team/sub/bot", "path": "app.py", "ref": SHA})
    assert linked.status_code == 200, linked.text
    assert repos.json()["repositories"][0]["full_name"] == "team/sub/bot"
    assert branches.json() == {"default_branch": "main", "branches": ["main", "dev"]}
    assert tree.json()["entries"] == [{"path": "app.py", "type": "file"}]
    assert file.json()["text"] == "print(1)\n"
    assert commits.json() == {"commits": [{"sha": SHA, "title": "First", "date": "2026-11-01T00:00:00Z"}]}


@pytest.mark.usefixtures("vault_key")
def test_bitbucket_picker_routes_list_and_tree() -> None:
    """A linked Bitbucket account lists repositories, branches and the tree."""
    client, _ = _make_client()
    with patch("core.connectors.transport.CLIENT.request", side_effect=_bitbucket_api):
        linked = client.put("/connectors/bitbucket/credentials", json={"fields": {"token": "bb-x"}})
        repos = client.get("/connectors/bitbucket/repos")
        branches = client.get("/connectors/bitbucket/branches", params={"repo": "team/bot"})
        tree = client.get("/connectors/bitbucket/tree", params={"repo": "team/bot"})
    assert linked.status_code == 200, linked.text
    assert [r["full_name"] for r in repos.json()["repositories"]] == ["team/bot"]
    assert branches.json() == {"default_branch": "main", "branches": ["main"]}
    assert tree.json()["entries"] == [{"path": "app.py", "type": "file"}]


@pytest.mark.usefixtures("vault_key")
@pytest.mark.parametrize("provider", ["gitlab", "bitbucket"])
def test_host_picker_needs_a_linked_account(provider: str) -> None:
    """Every host picker route answers 409 before the host is linked."""
    client, _ = _make_client()
    for route, params in (
        ("repos", {}),
        ("branches", {"repo": "team/bot"}),
        ("tree", {"repo": "team/bot"}),
        ("commits", {"repo": "team/bot"}),
        ("file", {"repo": "team/bot", "path": "README.md"}),
    ):
        response = client.get(f"/connectors/{provider}/{route}", params=params)
        assert response.status_code == 409, (route, response.text)
        assert response.json()["code"] == "connectors.not_connected"


@pytest.mark.usefixtures("vault_key")
def test_host_picker_refuses_other_providers() -> None:
    """Only GitLab and Bitbucket reach the shared host routes."""
    client, _ = _make_client()
    response = client.get("/connectors/notion/repos")
    assert response.status_code == 422


def _link(store: Any, provider: str, expires_in: timedelta, refresh_token: str | None = "rt") -> ConnectorVault:
    """Store an OAuth grant for alice on ``provider``.

    Args:
        store: The test store from ``_make_client``.
        provider: Connector name.
        expires_in: Time until the access token expires; negative for a stale token.
        refresh_token: The stored refresh token, or ``None``.

    Returns:
        The vault that holds the grant.
    """
    vault = ConnectorVault(store.engine)
    vault.save(
        "alice",
        provider,
        access_token="old",
        auth_method="oauth",
        account_label="alice",
        refresh_token=refresh_token,
        expires_at=datetime.now(UTC) + expires_in,
    )
    return vault


@pytest.mark.usefixtures("vault_key")
@pytest.mark.parametrize(("provider", "name"), [("github", "GitHub"), ("bitbucket", "Bitbucket")])
def test_revoked_token_gives_the_github_error_shape(provider: str, name: str) -> None:
    """A revoked Bitbucket token answers exactly like a revoked GitHub token: 409 ``connectors.rejected``."""
    client, store = _make_client()
    _link(store, provider, timedelta(hours=1))
    with patch("core.connectors.transport.CLIENT.request", return_value=_response(401, {"message": "revoked"})):
        response = client.get(f"/connectors/{provider}/repos")
    assert response.status_code == 409, response.text
    body = response.json()
    assert (body["code"], body["params"]) == ("connectors.rejected", {"provider": name})


@pytest.mark.usefixtures("vault_key")
@pytest.mark.parametrize("provider", ["github", "bitbucket"])
@pytest.mark.parametrize("refresh_token", ["rt", None])
def test_expired_grant_gives_the_github_error_shape(
    monkeypatch: pytest.MonkeyPatch, provider: str, refresh_token: str | None
) -> None:
    """An expired Bitbucket grant whose refresh fails, or with no refresh token, answers like GitHub's."""
    monkeypatch.setattr(settings, f"{provider}_oauth_client_id", "client")
    client, store = _make_client()
    vault = _link(store, provider, -timedelta(minutes=1), refresh_token)
    with (
        patch("core.connectors.oauth.httpx.post", return_value=_response(400, {"error": "invalid_grant"})),
        patch("core.connectors.transport.CLIENT.request") as never,
    ):
        response = client.get(f"/connectors/{provider}/repos")
    never.assert_not_called()
    assert response.status_code == 409, response.text
    assert response.json()["code"] == "connectors.expired"
    assert vault.get("alice", provider).status == "invalid"


@pytest.mark.usefixtures("vault_key")
def test_self_managed_gitlab_url_is_refused_on_the_route() -> None:
    """A self-managed GitLab URL answers 400 with a message that names the host and gitlab.com."""
    client, _ = _make_client()
    with patch("core.connectors.transport.CLIENT.request", side_effect=_gitlab_api):
        client.put("/connectors/gitlab/credentials", json={"fields": {"token": "glpat-x"}})
        response = client.get("/connectors/gitlab/branches", params={"repo": "https://gitlab.example.com/team/bot"})
    assert response.status_code == 400, response.text
    body = response.json()
    assert body["code"] == "connectors.other_repo_host"
    assert body["params"] == {"provider": "GitLab", "host": "gitlab.example.com", "expected": "gitlab.com"}
