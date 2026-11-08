"""Tests for the Bitbucket Cloud repository host, against a stubbed Bitbucket REST API."""

from __future__ import annotations

import json
from collections.abc import Callable

import httpx
import pytest

from ...api.errors import DomainError
from .. import bitbucket, transport
from ..github_publish import RepoPublishError
from ..github_repo import RepoFetchError
from ..vault import ConnectorSecret

SHA = "b" * 40
REPO = f"{bitbucket.API_URL}/repositories/team/bot"
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


def _secret() -> ConnectorSecret:
    """Build a stored Bitbucket connection.

    Returns:
        The secret.
    """
    return ConnectorSecret(access_token="bbtok", refresh_token=None, expires_at=None, auth_method="oauth")


def _repo_meta(request: httpx.Request) -> httpx.Response | None:
    """Answer the repository and commit lookups that most reads start with.

    Args:
        request: The outgoing request.

    Returns:
        The response, or ``None`` for any other URL.
    """
    url = str(request.url).split("?")[0]
    if url == REPO:
        return httpx.Response(200, json={"full_name": "team/bot", "mainbranch": {"name": "main"}})
    if url.startswith(f"{REPO}/commit/"):
        return httpx.Response(200, json={"hash": SHA})
    return None


def test_repo_url_rejects_a_bare_name() -> None:
    """A path without a workspace is refused before any request."""
    with pytest.raises(DomainError) as caught:
        bitbucket._repo_url("bot")
    assert caught.value.code == "connectors.invalid_ref"


def test_list_repositories_filters_and_finds_a_public_repository(monkeypatch: pytest.MonkeyPatch) -> None:
    """Member repositories are filtered by the search; an exact path outside the list is looked up."""

    def answer(request: httpx.Request) -> httpx.Response:
        """Return the member list or the direct lookup."""
        if request.url.path == "/2.0/repositories":
            values = [
                {"full_name": "team/bot", "is_private": True, "mainbranch": {"name": "main"}},
                {"full_name": "team/web", "is_private": True},
            ]
            return httpx.Response(200, json={"values": values})
        return httpx.Response(200, json={"full_name": "other/bot", "is_private": False})

    seen = _serve(monkeypatch, answer)
    listed = bitbucket.list_repositories(_secret(), "bot")
    assert [(r["full_name"], r["default_branch"]) for r in listed] == [("team/bot", "main")]
    assert seen[0].url.params["role"] == "member"
    assert seen[0].headers["Authorization"] == "Bearer bbtok"
    found = bitbucket.list_repositories(_secret(), "other/bot")
    assert (found[0]["full_name"], found[0]["private"]) == ("other/bot", False)


def test_pages_follows_only_api_next_links(monkeypatch: pytest.MonkeyPatch) -> None:
    """A ``next`` link on the API is followed; one pointing elsewhere ends the list."""

    def answer(request: httpx.Request) -> httpx.Response:
        """Return two linked pages, the second pointing off the API."""
        if request.url.params.get("page") == "2":
            return httpx.Response(200, json={"values": [{"name": "b"}], "next": "https://evil.example/2.0/x"})
        return httpx.Response(200, json={"values": [{"name": "a"}], "next": f"{REPO}/refs/branches?page=2"})

    seen = _serve(monkeypatch, answer)
    values, more = bitbucket._pages(f"{REPO}/refs/branches", "bbtok", {"pagelen": 10}, 5)
    assert ([v["name"] for v in values], more) == (["a", "b"], False)
    assert all(r.url.host == "api.bitbucket.org" for r in seen)


def test_list_branches_keeps_the_main_branch(monkeypatch: pytest.MonkeyPatch) -> None:
    """The main branch leads the list when the pages miss it."""

    def answer(request: httpx.Request) -> httpx.Response:
        """Return the repository or the branch page."""
        return _repo_meta(request) or httpx.Response(200, json={"values": [{"name": "dev"}]})

    _serve(monkeypatch, answer)
    assert bitbucket.list_branches(_secret(), "team/bot") == {"default_branch": "main", "branches": ["main", "dev"]}


def test_commit_passes_a_hash_through_and_pins_a_branch(monkeypatch: pytest.MonkeyPatch) -> None:
    """A full hash needs no request; a branch with a slash is encoded and pinned."""
    seen = _serve(monkeypatch, lambda r: _repo_meta(r) or httpx.Response(500))
    assert bitbucket._commit("bbtok", "team/bot", SHA) == SHA
    assert seen == []
    assert bitbucket._commit("bbtok", "team/bot", "feat/x") == SHA
    assert seen[0].url.raw_path.decode().endswith("/commit/feat%2Fx")


def test_tree_entries_maps_files_and_folders(monkeypatch: pytest.MonkeyPatch) -> None:
    """The ``/src`` listing at the pinned hash becomes files with sizes and folders."""

    def answer(request: httpx.Request) -> httpx.Response:
        """Return the repository, the commit or the tree."""
        values = [
            {"path": "src", "type": "commit_directory"},
            {"path": "src/app.py", "type": "commit_file", "size": 12},
            {"path": "link", "type": "commit_link"},
        ]
        return _repo_meta(request) or httpx.Response(200, json={"values": values})

    seen = _serve(monkeypatch, answer)
    assert bitbucket.tree_entries("bbtok", "team/bot", "") == {
        "entries": [{"path": "src", "type": "dir"}, {"path": "src/app.py", "type": "file", "size": 12}],
        "truncated": False,
    }
    assert seen[-1].url.path == f"/2.0/repositories/team/bot/src/{SHA}/"
    assert seen[-1].url.params["max_depth"] == str(bitbucket.TREE_DEPTH)


def test_tree_entries_reports_a_cut_tree(monkeypatch: pytest.MonkeyPatch) -> None:
    """A tree with pages left past the limit is marked truncated."""
    monkeypatch.setattr(bitbucket, "TREE_PAGE_LIMIT", 1)
    body = {"values": [{"path": "a", "type": "commit_file"}], "next": f"{REPO}/src/{SHA}/?page=2"}
    _serve(monkeypatch, lambda _r: httpx.Response(200, json=body))
    assert bitbucket.tree_entries("bbtok", "team/bot", SHA)["truncated"] is True


def test_file_at_reads_meta_then_bytes(monkeypatch: pytest.MonkeyPatch) -> None:
    """A small file is read after its metadata; a large one only by size."""

    def answer(request: httpx.Request) -> httpx.Response:
        """Return the metadata or the raw bytes."""
        size = 900 if "big" in request.url.path else 6
        if request.url.params.get("format") == "meta":
            return httpx.Response(200, json={"type": "commit_file", "size": size})
        return httpx.Response(200, content=b"hello\n")

    _serve(monkeypatch, answer)
    assert bitbucket.file_at("bbtok", "team/bot", SHA, "a.txt", 100) == {
        "content": b"hello\n",
        "size": 6,
        "missing": False,
        "too_large": False,
    }
    assert bitbucket.file_at("bbtok", "team/bot", SHA, "big.bin", 100)["too_large"] is True


def test_file_at_reports_a_folder_or_absent_path_as_missing(monkeypatch: pytest.MonkeyPatch) -> None:
    """A folder or a 404 is missing; other refusals still raise."""

    def answer(request: httpx.Request) -> httpx.Response:
        """Return a folder, a 404 or a refusal by path."""
        if "dir" in request.url.path:
            return httpx.Response(200, json={"type": "commit_directory"})
        if "gone" in request.url.path:
            return httpx.Response(404, json={})
        return httpx.Response(403, json={})

    _serve(monkeypatch, answer)
    assert bitbucket.file_at("bbtok", "team/bot", SHA, "dir", 100)["missing"] is True
    assert bitbucket.file_at("bbtok", "team/bot", SHA, "gone.py", 100)["missing"] is True
    with pytest.raises(DomainError) as caught:
        bitbucket.file_at("bbtok", "team/bot", SHA, "secret.py", 100)
    assert caught.value.code == "connectors.rejected"


def test_resolve_commit_failure_is_a_fetch_error(monkeypatch: pytest.MonkeyPatch) -> None:
    """A refused lookup names the repository and the branch."""
    _serve(monkeypatch, lambda _r: httpx.Response(404, json={}))
    with pytest.raises(RepoFetchError, match="team/bot at 'dev'"):
        bitbucket.resolve_commit("bbtok", "team/bot", "dev")


def test_open_change_opens_a_draft_pull_request(monkeypatch: pytest.MonkeyPatch) -> None:
    """The pull request is sent as a draft into the main branch."""

    def answer(request: httpx.Request) -> httpx.Response:
        """Return the repository or the opened pull request."""
        opened = {"id": 4, "draft": True, "links": {"html": {"href": "https://bitbucket.org/team/bot/pull-requests/4"}}}
        return _repo_meta(request) or httpx.Response(201, json=opened)

    seen = _serve(monkeypatch, answer)
    change = bitbucket.open_change("bbtok", "team/bot", head="skynet/best", base_branch=None, title="T", body="B")
    assert (change.number, change.draft, change.branch) == (4, True, "skynet/best")
    sent = json.loads(seen[-1].read())
    assert sent["draft"] is True
    assert sent["destination"] == {"branch": {"name": "main"}}
    assert sent["source"] == {"branch": {"name": "skynet/best"}}


def test_open_change_falls_back_when_drafts_are_refused(monkeypatch: pytest.MonkeyPatch) -> None:
    """A 400 on the draft request retries once without the draft field."""

    def answer(request: httpx.Request) -> httpx.Response:
        """Refuse the draft, accept the regular pull request."""
        if json.loads(request.read()).get("draft"):
            return httpx.Response(400, json={"error": {"message": "draft"}})
        opened = {"id": 5, "links": {"html": {"href": "https://bitbucket.org/team/bot/pull-requests/5"}}}
        return httpx.Response(201, json=opened)

    seen = _serve(monkeypatch, answer)
    change = bitbucket.open_change("bbtok", "team/bot", head="h", base_branch="main", title="T", body="B")
    assert (change.number, change.draft) == (5, False)
    assert len(seen) == 2


def test_open_change_refusal_is_a_publish_error(monkeypatch: pytest.MonkeyPatch) -> None:
    """A refusal other than the draft 400 is not retried."""
    seen = _serve(monkeypatch, lambda _r: httpx.Response(403, json={}))
    with pytest.raises(RepoPublishError, match="pull request"):
        bitbucket.open_change("bbtok", "team/bot", head="h", base_branch="main", title="T", body="B")
    assert len(seen) == 1


def test_oauth_app_uses_bitbucket_endpoints() -> None:
    """The OAuth consumer points at bitbucket.org and sends its secret by basic auth."""
    app = bitbucket.oauth_app()
    assert app.authorize_url == "https://bitbucket.org/site/oauth2/authorize"
    assert app.token_url == "https://bitbucket.org/site/oauth2/access_token"
    assert app.basic_auth_form is True


def test_list_commits_names_the_branch_in_the_query(monkeypatch: pytest.MonkeyPatch) -> None:
    """A branch with a slash goes in ``include``; each commit keeps its first message line."""
    body = {"values": [{"hash": SHA, "message": "Tune prompt\nmore", "date": "2026-11-02T08:00:00+00:00"}]}
    seen = _serve(monkeypatch, lambda _r: httpx.Response(200, json=body))
    assert bitbucket.list_commits(_secret(), "team/bot", "feature/x") == [
        {"sha": SHA, "title": "Tune prompt", "date": "2026-11-02T08:00:00+00:00"}
    ]
    assert seen[0].url.path == "/2.0/repositories/team/bot/commits"
    assert seen[0].url.params["include"] == "feature/x"


@pytest.mark.parametrize(
    ("value", "expected"),
    [
        ("https://user@BITBUCKET.org:443/team/bot.git", "team/bot"),
        ("https://bitbucket.org/team/bot/src/main/app.py", "team/bot"),
        ("git@bitbucket.org:team/bot.git", "team/bot"),
        ("team/bot.git", "team/bot"),
        ("https://bitbucket.org/team/bot/src/main/dir", "team/bot"),
        ("ssh://git@bitbucket.org/team/bot.git", "team/bot"),
        ("https://bitbucket.org/team/bot?at=main#readme", "team/bot"),
        ("team/bot/", "team/bot"),
        ("https://bitbucket.org/team/bot.git/?at=main", "team/bot"),
    ],
)
def test_repo_path_reads_a_pasted_url(value: str, expected: str) -> None:
    """A port, a user, a trailing ``.git``, upper-case host letters and page paths do not change the repository."""
    assert bitbucket.repo_path(value, bitbucket.WEB_URL, bitbucket.PROVIDER, segments=2) == expected
    assert bitbucket._repo_url(value) == REPO


def test_other_host_is_refused_by_name(monkeypatch: pytest.MonkeyPatch) -> None:
    """A URL on another host (for example Bitbucket Data Center) is refused by name, before any request."""
    seen = _serve(monkeypatch, lambda _r: httpx.Response(200, json={"values": []}))
    with pytest.raises(DomainError) as caught:
        bitbucket._repo_url("https://bitbucket.example.com/scm/team/bot.git")
    assert caught.value.code == "connectors.other_repo_host"
    assert caught.value.params["host"] == "bitbucket.example.com"
    assert caught.value.params["expected"] == "bitbucket.org"
    with pytest.raises(DomainError):
        bitbucket.list_repositories(_secret(), "https://bitbucket.example.com/team/bot")
    assert seen == []


@pytest.mark.parametrize(
    "value",
    [
        "https://bitbucket.org/",
        "https://bitbucket.org/team",
        "https://[::1/team/bot",
        "../bot",
        "team/..",
        "https://bitbucket.org/team/../other/bot",
        "https://bitbucket.org/צוות/bot",
        "https://bitbucket.org/café/bot",
    ],
)
def test_repo_url_refuses_an_unusable_url_with_400(value: str) -> None:
    """A URL without a workspace and repository, or a broken URL, is a 400 ``invalid_ref``."""
    with pytest.raises(DomainError) as caught:
        bitbucket._repo_url(value)
    assert (caught.value.code, caught.value.status_code) == ("connectors.invalid_ref", 400)


@pytest.mark.parametrize("search", [".", "team/..", "../bot"])
def test_dot_search_lists_without_a_direct_lookup(monkeypatch: pytest.MonkeyPatch, search: str) -> None:
    """A search with a ``.`` or ``..`` segment filters the list and skips the direct repository lookup."""
    seen = _serve(monkeypatch, lambda _r: httpx.Response(200, json={"values": []}))
    assert bitbucket.list_repositories(_secret(), search) == []
    assert [r.url.path for r in seen] == ["/2.0/repositories"]
