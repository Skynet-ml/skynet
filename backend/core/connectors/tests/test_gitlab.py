"""Tests for the gitlab.com repository host, against a stubbed GitLab REST API."""

from __future__ import annotations

import base64
import json
from collections.abc import Callable
from typing import Any

import httpx
import pytest

from ...api.errors import DomainError
from ...i18n import t_locale
from .. import gitlab, transport
from ..github_publish import HardeningSummary, RepoPublishError
from ..github_repo import RepoFetchError
from ..vault import ConnectorSecret

SHA = "a" * 40
Handler = Callable[[httpx.Request], httpx.Response]


def _serve(monkeypatch: pytest.MonkeyPatch, handler: Handler) -> list[httpx.Request]:
    """Route every connector HTTP call to ``handler`` and record the requests.

    Args:
        monkeypatch: Pytest monkeypatch fixture.
        handler: Returns the canned response of one request.

    Returns:
        The requests, in order.
    """
    seen: list[httpx.Request] = []

    def record(request: httpx.Request) -> httpx.Response:
        """Record the request and answer it.

        Args:
            request: The outgoing request.

        Returns:
            The handler's response.
        """
        seen.append(request)
        return handler(request)

    monkeypatch.setattr(transport, "CLIENT", httpx.Client(transport=httpx.MockTransport(record)))
    return seen


def _secret(token: str = "glpat") -> ConnectorSecret:
    """Build a stored GitLab connection.

    Args:
        token: The access token.

    Returns:
        The secret.
    """
    return ConnectorSecret(access_token=token, refresh_token=None, expires_at=None, auth_method="oauth")


def _project(path: str, **extra: Any) -> dict[str, Any]:
    """Build a GitLab project object.

    Args:
        path: ``path_with_namespace``.
        **extra: Fields to add or override.

    Returns:
        The project.
    """
    return {"path_with_namespace": path, "visibility": "private", "default_branch": "main", **extra}


def test_project_path_is_one_encoded_segment(monkeypatch: pytest.MonkeyPatch) -> None:
    """A subgroup path goes into the URL as one encoded id, with the bearer token."""
    seen = _serve(monkeypatch, lambda _r: httpx.Response(200, json={"default_branch": "dev"}))
    assert gitlab._default_branch("glpat", "group/sub/app") == "dev"
    assert seen[0].url.raw_path.decode() == "/api/v4/projects/group%2Fsub%2Fapp"
    assert seen[0].headers["Authorization"] == "Bearer glpat"


def test_project_path_rejects_a_bare_name() -> None:
    """A path without a namespace is refused before any request."""
    with pytest.raises(DomainError) as caught:
        gitlab._project_url("app")
    assert caught.value.code == "connectors.invalid_ref"


def test_list_repositories_filters_and_finds_a_public_project(monkeypatch: pytest.MonkeyPatch) -> None:
    """Member projects are filtered by the search; an exact path outside the list is looked up."""

    def answer(request: httpx.Request) -> httpx.Response:
        """Return the member list or the direct lookup."""
        if request.url.path == "/api/v4/projects":
            return httpx.Response(200, json=[_project("team/bot", description="Chat bot"), _project("team/web")])
        return httpx.Response(200, json=_project("other/bot", visibility="public"))

    seen = _serve(monkeypatch, answer)
    assert [r["full_name"] for r in gitlab.list_repositories(_secret(), "bot")] == ["team/bot"]
    found = gitlab.list_repositories(_secret(), "other/bot")
    assert found[0] == {
        "full_name": "other/bot",
        "private": False,
        "description": None,
        "language": None,
        "default_branch": "main",
        "pushed_at": None,
    }
    assert seen[0].url.params["membership"] == "true"


def test_list_branches_pages_and_keeps_the_default(monkeypatch: pytest.MonkeyPatch) -> None:
    """Branches come from every page; the default branch leads when the pages miss it."""

    def answer(request: httpx.Request) -> httpx.Response:
        """Return the project or one page of branches."""
        if request.url.path.endswith("/repository/branches"):
            page = int(request.url.params["page"])
            names = [f"b{page}-{i}" for i in range(gitlab.PAGE_SIZE if page == 1 else 2)]
            return httpx.Response(200, json=[{"name": n} for n in names])
        return httpx.Response(200, json={"default_branch": "main"})

    _serve(monkeypatch, answer)
    result = gitlab.list_branches(_secret(), "team/bot")
    assert result["default_branch"] == "main"
    assert result["branches"][0] == "main"
    assert len(result["branches"]) == gitlab.PAGE_SIZE + 3


def test_list_commits_keeps_the_first_message_line(monkeypatch: pytest.MonkeyPatch) -> None:
    """Commits come from the branch, with the message's first line as the title."""
    body = [
        {"id": SHA, "message": "Fix the scorer\n\nLong body", "committed_date": "2026-11-01T10:00:00Z"},
        {"title": "no id"},
    ]
    seen = _serve(monkeypatch, lambda _r: httpx.Response(200, json=body))
    assert gitlab.list_commits(_secret(), "team/sub/bot", " dev ") == [
        {"sha": SHA, "title": "Fix the scorer", "date": "2026-11-01T10:00:00Z"}
    ]
    assert seen[0].url.raw_path.decode().startswith("/api/v4/projects/team%2Fsub%2Fbot/repository/commits")
    assert seen[0].url.params["ref_name"] == "dev"


def test_tree_entries_maps_blobs_and_trees(monkeypatch: pytest.MonkeyPatch) -> None:
    """The recursive tree becomes files and folders at the requested ref."""
    body = [
        {"path": "src", "type": "tree"},
        {"path": "src/app.py", "type": "blob"},
        {"path": "vendor", "type": "commit"},
    ]
    seen = _serve(monkeypatch, lambda _r: httpx.Response(200, json=body))
    assert gitlab.tree_entries("glpat", "team/bot", " dev ") == {
        "entries": [{"path": "src", "type": "dir"}, {"path": "src/app.py", "type": "file"}],
        "truncated": False,
    }
    assert seen[0].url.params["ref"] == "dev"
    assert seen[0].url.params["recursive"] == "true"


def test_tree_entries_reports_a_cut_tree(monkeypatch: pytest.MonkeyPatch) -> None:
    """A tree longer than the page limit is marked truncated."""
    monkeypatch.setattr(gitlab, "TREE_PAGE_LIMIT", 2)
    full = [{"path": f"f{i}", "type": "blob"} for i in range(gitlab.PAGE_SIZE)]
    _serve(monkeypatch, lambda _r: httpx.Response(200, json=full))
    result = gitlab.tree_entries("glpat", "team/bot", "")
    assert result["truncated"] is True
    assert len(result["entries"]) == 2 * gitlab.PAGE_SIZE


def test_file_at_decodes_base64_and_reports_missing(monkeypatch: pytest.MonkeyPatch) -> None:
    """A file is decoded; an absent path is missing, not an error."""

    def answer(request: httpx.Request) -> httpx.Response:
        """Return the file or a 404."""
        if "missing" in request.url.raw_path.decode():
            return httpx.Response(404, json={"message": "404 File Not Found"})
        content = base64.b64encode(b"print(1)\n").decode()
        return httpx.Response(200, json={"content": content, "encoding": "base64", "size": 9})

    seen = _serve(monkeypatch, answer)
    assert gitlab.file_at("glpat", "team/bot", SHA, "src/app.py", 100) == {
        "content": b"print(1)\n",
        "size": 9,
        "missing": False,
        "too_large": False,
    }
    assert seen[0].url.raw_path.decode().endswith("/repository/files/src%2Fapp.py?ref=" + SHA)
    assert gitlab.file_at("glpat", "team/bot", SHA, "missing.py", 100)["missing"] is True
    assert gitlab.read_repo_file("glpat", "team/bot", SHA, "src/app.py", 100)["text"] == "print(1)\n"


def test_file_at_skips_a_large_file(monkeypatch: pytest.MonkeyPatch) -> None:
    """A file past the cap is reported by size and not decoded."""
    _serve(monkeypatch, lambda _r: httpx.Response(200, json={"content": "", "encoding": "base64", "size": 500}))
    assert gitlab.file_at("glpat", "team/bot", SHA, "big.bin", 100) == {
        "content": None,
        "size": 500,
        "missing": False,
        "too_large": True,
    }


def test_read_text_file_streams_the_raw_file(monkeypatch: pytest.MonkeyPatch) -> None:
    """The raw endpoint is read up to the cap at the default branch."""

    def answer(request: httpx.Request) -> httpx.Response:
        """Return the project or the raw bytes."""
        if request.url.path.endswith("/raw"):
            return httpx.Response(200, content=b"hello world")
        return httpx.Response(200, json={"default_branch": "main"})

    seen = _serve(monkeypatch, answer)
    assert gitlab.read_text_file("glpat", "team/bot", "", "README.md", 5) == ("hello", True)
    assert seen[-1].url.params["ref"] == "main"


def test_resolve_commit_pins_the_default_branch(monkeypatch: pytest.MonkeyPatch) -> None:
    """With no branch, the default branch's commit id is returned."""

    def answer(request: httpx.Request) -> httpx.Response:
        """Return the project or the commit."""
        if "/repository/commits/" in request.url.path:
            return httpx.Response(200, json={"id": SHA})
        return httpx.Response(200, json={"default_branch": "main"})

    seen = _serve(monkeypatch, answer)
    assert gitlab.resolve_commit("glpat", "team/bot", None) == SHA
    assert seen[-1].url.path.endswith("/repository/commits/main")


def test_resolve_commit_failure_is_a_fetch_error(monkeypatch: pytest.MonkeyPatch) -> None:
    """A refused lookup names the project and the branch."""
    _serve(monkeypatch, lambda _r: httpx.Response(404, json={}))
    with pytest.raises(RepoFetchError, match="team/bot at 'dev'"):
        gitlab.resolve_commit("glpat", "team/bot", "dev")


def test_open_change_opens_a_draft_merge_request(monkeypatch: pytest.MonkeyPatch) -> None:
    """The merge request gets the draft prefix, the target branch and the hardening line."""
    opened = {"web_url": "https://gitlab.com/team/bot/-/merge_requests/7", "iid": 7, "draft": True}
    seen = _serve(monkeypatch, lambda _r: httpx.Response(201, json=opened))
    change = gitlab.open_change(
        "glpat",
        "team/bot",
        head="skynet/best",
        base_branch="main",
        title="Better prompt",
        body="Body",
        hardening=HardeningSummary(attacks=2, exploits_fixed=1),
    )
    assert (change.url, change.number, change.draft, change.branch) == (opened["web_url"], 7, True, "skynet/best")
    sent = json.loads(seen[0].read())
    assert seen[0].method == "POST"
    assert (sent["title"], sent["source_branch"], sent["target_branch"]) == (
        "Draft: Better prompt",
        "skynet/best",
        "main",
    )
    assert "Metric survived 2 attacks, 1 exploit fixed" in sent["description"]


def test_open_change_refusal_is_a_publish_error(monkeypatch: pytest.MonkeyPatch) -> None:
    """A refused merge request becomes a publish error the owner can read."""
    _serve(monkeypatch, lambda _r: httpx.Response(409, json={"message": ["exists"]}))
    with pytest.raises(RepoPublishError, match="merge request"):
        gitlab.open_change("glpat", "team/bot", head="h", base_branch="main", title="t", body="b")


def test_oauth_app_uses_gitlab_endpoints() -> None:
    """The OAuth app points at gitlab.com and asks for the api scope."""
    app = gitlab.oauth_app()
    assert app.authorize_url == "https://gitlab.com/oauth/authorize"
    assert app.token_url == "https://gitlab.com/oauth/token"
    assert "api" in app.scopes.split()


@pytest.mark.parametrize(
    ("value", "expected"),
    [
        ("https://GitLab.com:443/team/sub/bot.git", "team/sub/bot"),
        ("https://www.gitlab.com/team/bot/-/tree/main/src", "team/bot"),
        ("git@gitlab.com:team/bot.git", "team/bot"),
        ("  team/bot.git ", "team/bot"),
        ("ssh://git@gitlab.com:22/group/sub/repo.git", "group/sub/repo"),
        ("group/sub/sub2/repo", "group/sub/sub2/repo"),
        ("https://gitlab.com/group/sub/sub2/repo", "group/sub/sub2/repo"),
        ("https://gitlab.com/team/bot?tab=readme", "team/bot"),
        ("https://gitlab.com/team/bot#readme", "team/bot"),
        ("https://gitlab.com/team/bot/", "team/bot"),
        ("team/bot/", "team/bot"),
        ("https://gitlab.com/team/my%2Dbot", "team/my-bot"),
    ],
)
def test_repo_path_reads_a_pasted_url(value: str, expected: str) -> None:
    """A port, a trailing ``.git``, upper-case host letters and page paths do not change the project."""
    assert gitlab.repo_path(value, gitlab.WEB_URL, gitlab.PROVIDER) == expected


def test_project_url_accepts_a_clone_url(monkeypatch: pytest.MonkeyPatch) -> None:
    """A clone URL resolves to the same encoded project id as its path."""
    seen = _serve(monkeypatch, lambda _r: httpx.Response(200, json={"default_branch": "dev"}))
    assert gitlab._default_branch("glpat", "https://GITLAB.com:443/group/sub/app.git") == "dev"
    assert seen[0].url.raw_path.decode() == "/api/v4/projects/group%2Fsub%2Fapp"


def test_list_repositories_finds_a_project_from_its_url(monkeypatch: pytest.MonkeyPatch) -> None:
    """A pasted web URL finds the project, like its path does."""

    def answer(request: httpx.Request) -> httpx.Response:
        """Return an empty member list or the direct lookup."""
        if request.url.path == "/api/v4/projects":
            return httpx.Response(200, json=[])
        return httpx.Response(200, json=_project("other/bot", visibility="public"))

    _serve(monkeypatch, answer)
    found = gitlab.list_repositories(_secret(), "https://gitlab.com/other/bot.git")
    assert [r["full_name"] for r in found] == ["other/bot"]


@pytest.mark.parametrize("value", ["https://gitlab.example.com/team/bot", "git@gitlab.example.com:team/bot.git"])
def test_self_managed_gitlab_host_is_refused_by_name(monkeypatch: pytest.MonkeyPatch, value: str) -> None:
    """A self-managed GitLab URL gets a refusal that names its host and gitlab.com, before any request."""
    seen = _serve(monkeypatch, lambda _r: httpx.Response(200, json={}))
    with pytest.raises(DomainError) as caught:
        gitlab._project_url(value)
    assert caught.value.code == "connectors.other_repo_host"
    assert caught.value.status_code == 400
    message = t_locale("en", caught.value.code, **caught.value.params)
    assert message.startswith("gitlab.example.com is not gitlab.com.")
    assert "Use the repository's gitlab.com address" in message
    assert "gitlab.example.com" in t_locale("he", caught.value.code, **caught.value.params)
    assert caught.value.params == {"provider": "GitLab", "host": "gitlab.example.com", "expected": "gitlab.com"}
    with pytest.raises(DomainError):
        gitlab.list_repositories(_secret(), value)
    assert len(seen) == 1


@pytest.mark.parametrize(
    "value",
    [
        "https://gitlab.com/",
        "https://gitlab.com",
        "https://",
        "https://[::1/team/bot",
        "https://gitlab.com/team/my%20bot",
        "",
    ],
)
def test_project_url_refuses_an_unusable_url_with_400(value: str) -> None:
    """An empty, host-less, broken or invalid-character URL is a 400 ``invalid_ref``, never a crash."""
    with pytest.raises(DomainError) as caught:
        gitlab._project_url(value)
    assert (caught.value.code, caught.value.status_code) == ("connectors.invalid_ref", 400)
