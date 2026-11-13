"""Tests for the repository-run file routes under ``/optimizations/{id}/repository``."""

from __future__ import annotations

import base64
from collections.abc import Iterator
from typing import Any
from unittest.mock import patch
from urllib.parse import urlparse

import httpx
import pytest
from fastapi import FastAPI
from fastapi.responses import JSONResponse
from fastapi.testclient import TestClient

from ...connectors.github_repo import RepoFetchError
from ..auth import AuthenticatedUser
from ..errors import DomainError
from ..routers.optimizations import create_optimizations_router
from ..routers.optimizations import repository as repository_module
from .conftest import bypass_auth
from .mocks import FakeJobStore

COMMIT = "c" * 40
TREE_PATH = f"/repos/acme/app/git/trees/{COMMIT}"


def _response(status: int, body: Any) -> httpx.Response:
    """Build a JSON response with a request attached.

    Args:
        status: HTTP status.
        body: JSON body.

    Returns:
        The response.
    """
    return httpx.Response(status, json=body, request=httpx.Request("GET", "https://api.github.com/"))


def _file(content: bytes, size: int | None = None) -> dict[str, Any]:
    """Shape a contents-API file reply.

    Args:
        content: The file bytes.
        size: Reported size; defaults to the content length.

    Returns:
        The reply body.
    """
    return {
        "type": "file",
        "size": len(content) if size is None else size,
        "encoding": "base64",
        "content": base64.b64encode(content).decode(),
    }


class _FakeGithub:
    """Answer GitHub API calls by path and record what was asked."""

    def __init__(self) -> None:
        """Start with no recorded calls."""
        self.calls: list[tuple[str, dict[str, Any] | None, str | None]] = []

    def __call__(self, method: str, url: str, **kwargs: Any) -> httpx.Response:
        """Route one call.

        Args:
            method: HTTP method.
            url: Request URL.
            **kwargs: Request options; ``params`` and ``headers`` are read.

        Returns:
            The canned response.
        """
        path = urlparse(url).path
        params = kwargs.get("params")
        auth = (kwargs.get("headers") or {}).get("Authorization")
        self.calls.append((path, params, auth))
        if auth == "Bearer revoked":
            return _response(401, {"message": "Bad credentials"})
        if path == TREE_PATH:
            return _response(
                200,
                {
                    "truncated": False,
                    "tree": [
                        {"path": "src/main.py", "type": "blob", "size": 12},
                        {"path": "README.md", "type": "blob", "size": 5},
                        {"path": "src", "type": "tree"},
                        {"path": "vendor/lib", "type": "commit"},
                    ],
                },
            )
        assert params == {"ref": COMMIT}
        files = {
            "/repos/acme/app/contents/src/main.py": _file(b"print('hi')\n"),
            "/repos/acme/app/contents/logo.png": _file(b"\x89PNG\x00\x01"),
            "/repos/acme/app/contents/latin1.txt": _file("café".encode("latin-1")),
            "/repos/acme/app/contents/big.bin": {"type": "file", "size": 5 * 1024 * 1024, "encoding": "none"},
        }
        if path in files:
            return _response(200, files[path])
        return _response(404, {"message": "Not Found"})


@pytest.fixture(autouse=True)
def _empty_caches() -> Iterator[None]:
    """Start and end every test with empty route caches."""
    repository_module._trees.clear()
    repository_module._files.clear()
    yield
    repository_module._trees.clear()
    repository_module._files.clear()


@pytest.fixture
def store() -> FakeJobStore:
    """Seed one repository run owned by alice and one plain run.

    Returns:
        The store.
    """
    jobs = FakeJobStore()
    jobs.seed_job(
        "repo-run",
        payload={
            "username": "alice",
            "target": {"kind": "repo", "repo": {"repository": "acme/app", "commit": COMMIT, "editable_paths": ["."]}},
        },
    )
    jobs.seed_job("text-run", payload={"username": "alice", "target": {"kind": "text"}})
    jobs.seed_job(
        "unpinned-run",
        payload={"username": "alice", "target": {"kind": "repo", "repo": {"repository": "acme/app"}}},
    )
    return jobs


def _client(store: FakeJobStore, user: AuthenticatedUser | None = None) -> TestClient:
    """Mount the optimizations router over ``store``.

    Args:
        store: Fake job store.
        user: Caller; defaults to the admin test user.

    Returns:
        The client.
    """
    app = FastAPI()
    app.include_router(create_optimizations_router(job_store=store, get_worker_ref=lambda: None))
    bypass_auth(app, user=user)

    @app.exception_handler(DomainError)
    async def _domain_error_handler(_request, exc: DomainError) -> JSONResponse:
        """Mirror the app-level envelope so tests can assert on ``code``.

        Args:
            _request: Ignored request.
            exc: The raised error.

        Returns:
            The envelope.
        """
        return JSONResponse(status_code=exc.status_code, content={"detail": exc.detail, "code": exc.code})

    return TestClient(app, raise_server_exceptions=False)


@pytest.fixture
def github() -> Iterator[_FakeGithub]:
    """Patch the shared HTTP client with the fake GitHub.

    Yields:
        The fake, for call assertions.
    """
    fake = _FakeGithub()
    with patch("core.connectors.transport.CLIENT.request", side_effect=fake):
        yield fake


def test_tree_lists_every_path_at_the_commit_sorted(store: FakeJobStore, github: _FakeGithub) -> None:
    """The tree comes from the pinned commit, sorted, with sizes for files; a second read is cached."""
    client = _client(store)

    first = client.get("/optimizations/repo-run/repository/tree")
    second = client.get("/optimizations/repo-run/repository/tree")

    assert first.status_code == 200, first.text
    assert first.json() == {
        "repository": "acme/app",
        "commit": COMMIT,
        "entries": [
            {"path": "README.md", "type": "file", "size": 5},
            {"path": "src", "type": "dir", "size": None},
            {"path": "src/main.py", "type": "file", "size": 12},
        ],
        "truncated": False,
    }
    assert second.json() == first.json()
    assert [call[0] for call in github.calls] == [TREE_PATH]


def _seed_drafted_run(store: FakeJobStore) -> None:
    """Seed a run on the same commit whose setup draft edits one file and adds another.

    Args:
        store: Fake job store.
    """
    store.seed_job(
        "drafted-run",
        payload={
            "username": "alice",
            "target": {
                "kind": "repo",
                "repo": {
                    "repository": "acme/app",
                    "commit": COMMIT,
                    "editable_paths": ["."],
                    "draft_files": {"src/main.py": "drafted\n", "cfg/optimize.toml": "[run]\n"},
                },
            },
        },
    )


def test_tree_shows_the_run_draft_files_and_keeps_the_cache_clean(store: FakeJobStore, github: _FakeGithub) -> None:
    """A run's drafts show in its tree only; another run on the commit still sees the plain tree."""
    _seed_drafted_run(store)
    client = _client(store)

    drafted = client.get("/optimizations/drafted-run/repository/tree").json()
    plain = client.get("/optimizations/repo-run/repository/tree").json()

    assert drafted["entries"] == [
        {"path": "README.md", "type": "file", "size": 5},
        {"path": "cfg", "type": "dir", "size": None},
        {"path": "cfg/optimize.toml", "type": "file", "size": 6},
        {"path": "src", "type": "dir", "size": None},
        {"path": "src/main.py", "type": "file", "size": 8},
    ]
    assert [entry["path"] for entry in plain["entries"]] == ["README.md", "src", "src/main.py"]
    assert [call[0] for call in github.calls] == [TREE_PATH]


def test_file_route_returns_the_run_draft_text(store: FakeJobStore, github: _FakeGithub) -> None:
    """A draft file reads as the run's draft text, without a host read, and never fills the shared cache."""
    _seed_drafted_run(store)
    client = _client(store)

    drafted = client.get("/optimizations/drafted-run/repository/file", params={"path": "src/main.py"}).json()
    plain = client.get("/optimizations/repo-run/repository/file", params={"path": "src/main.py"}).json()

    assert drafted == {
        "path": "src/main.py",
        "content": "drafted\n",
        "binary": False,
        "too_large": False,
        "missing": False,
        "size": 8,
    }
    assert plain["content"] == "print('hi')\n"
    assert len(github.calls) == 1


def test_file_found_returns_its_text(store: FakeJobStore, github: _FakeGithub) -> None:
    """A text file comes back decoded, and stepping back to it does not refetch."""
    client = _client(store)

    body = client.get("/optimizations/repo-run/repository/file", params={"path": "src/main.py"}).json()
    client.get("/optimizations/repo-run/repository/file", params={"path": "src/main.py"})

    assert body == {
        "path": "src/main.py",
        "content": "print('hi')\n",
        "binary": False,
        "too_large": False,
        "missing": False,
        "size": 12,
    }
    assert len(github.calls) == 1


def test_file_missing_at_the_commit_is_not_an_error(store: FakeJobStore, github: _FakeGithub) -> None:
    """A path the commit lacks, as a version's new file, reads as missing."""
    response = _client(store).get("/optimizations/repo-run/repository/file", params={"path": "src/new.py"})

    assert response.status_code == 200
    assert response.json() == {
        "path": "src/new.py",
        "content": None,
        "binary": False,
        "too_large": False,
        "missing": True,
        "size": None,
    }


@pytest.mark.parametrize("path", ["logo.png", "latin1.txt"])
def test_binary_files_carry_no_content(store: FakeJobStore, github: _FakeGithub, path: str) -> None:
    """NUL bytes or invalid UTF-8 mark a file binary.

    Args:
        store: Seeded store.
        github: Fake GitHub.
        path: The binary file.
    """
    body = _client(store).get("/optimizations/repo-run/repository/file", params={"path": path}).json()

    assert body["binary"] is True
    assert body["content"] is None
    assert body["missing"] is False


def test_file_over_one_megabyte_is_too_large(store: FakeJobStore, github: _FakeGithub) -> None:
    """A file over the cap reports its size and no content."""
    body = _client(store).get("/optimizations/repo-run/repository/file", params={"path": "big.bin"}).json()

    assert body == {**body, "too_large": True, "content": None, "size": 5 * 1024 * 1024}


@pytest.mark.parametrize("path", ["../secrets", "src/../../x", "/etc/passwd", "C:/x", "a//b", "./a"])
def test_paths_outside_the_repository_are_refused(store: FakeJobStore, github: _FakeGithub, path: str) -> None:
    """Absolute and escaping paths get 400 without reaching GitHub.

    Args:
        store: Seeded store.
        github: Fake GitHub.
        path: The refused path.
    """
    response = _client(store).get("/optimizations/repo-run/repository/file", params={"path": path})

    assert response.status_code == 400
    assert response.json()["code"] == "connectors.invalid_ref"
    assert github.calls == []


@pytest.mark.parametrize("route", ["tree", "file?path=a.py"])
def test_non_repository_and_unknown_runs_are_not_found(store: FakeJobStore, github: _FakeGithub, route: str) -> None:
    """A run that does not optimize a repository, or does not exist, 404s.

    Args:
        store: Seeded store.
        github: Fake GitHub.
        route: The route suffix.
    """
    client = _client(store)

    assert client.get(f"/optimizations/text-run/repository/{route}").status_code == 404
    assert client.get(f"/optimizations/nope/repository/{route}").status_code == 404
    assert github.calls == []


def test_unpinned_run_conflicts(store: FakeJobStore, github: _FakeGithub) -> None:
    """A run whose commit staging has not pinned yet answers 409."""
    response = _client(store).get("/optimizations/unpinned-run/repository/tree")

    assert response.status_code == 409


@pytest.mark.parametrize("route", ["tree", "file?path=src/main.py"])
def test_access_matches_the_other_read_routes(store: FakeJobStore, github: _FakeGithub, route: str) -> None:
    """A caller who cannot open the run gets the same 404 as ``GET /optimizations/{id}``.

    Args:
        store: Seeded store.
        github: Fake GitHub.
        route: The route suffix.
    """
    client = _client(store, AuthenticatedUser(username="mallory", role="user", groups=()))

    detail = client.get("/optimizations/repo-run")
    response = client.get(f"/optimizations/repo-run/repository/{route}")

    assert detail.status_code == response.status_code == 404
    assert response.json()["code"] == detail.json()["code"] == "optimization.not_found"
    assert github.calls == []


def test_the_owner_token_reads_and_a_rejected_one_falls_back(
    store: FakeJobStore, github: _FakeGithub, monkeypatch: pytest.MonkeyPatch
) -> None:
    """The owner's connection is used; a rejected token retries anonymously for public repositories."""
    asked: list[str] = []

    def token(engine: Any, username: str) -> str:
        """Hand out the owner's token.

        Args:
            engine: Ignored vault engine.
            username: Whose token.

        Returns:
            A token GitHub rejects.
        """
        asked.append(username)
        return "revoked"

    store.engine = object()
    monkeypatch.setattr(repository_module, "github_token", token)

    response = _client(store).get("/optimizations/repo-run/repository/tree")

    assert response.status_code == 200
    assert asked == ["alice"]
    assert [call[2] for call in github.calls] == ["Bearer revoked", None]


def test_a_missing_owner_connection_reads_anonymously(
    store: FakeJobStore, github: _FakeGithub, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Without the owner's GitHub link a public repository still opens."""

    def no_token(engine: Any, username: str) -> str:
        """Report the owner has no connection.

        Args:
            engine: Ignored vault engine.
            username: Whose token.

        Raises:
            RepoFetchError: Always.
        """
        raise RepoFetchError("Connect GitHub again")

    store.engine = object()
    monkeypatch.setattr(repository_module, "github_token", no_token)

    response = _client(store).get("/optimizations/repo-run/repository/file", params={"path": "src/main.py"})

    assert response.status_code == 200
    assert github.calls[0][2] is None


def test_github_refusal_surfaces_as_a_domain_error(store: FakeJobStore) -> None:
    """A tree GitHub will not serve comes back as GitHub's not-found error the UI can show."""
    with patch(
        "core.connectors.transport.CLIENT.request",
        return_value=_response(404, {"message": "Not Found"}),
    ):
        response = _client(store).get("/optimizations/repo-run/repository/tree")

    assert response.status_code == 404
    assert response.json()["code"] == "connectors.not_found"


def test_archive_zips_only_a_recorded_version(store: FakeJobStore) -> None:
    """Zip a patch the run recorded; refuse one it never produced."""
    store.seed_job(
        "done-run",
        payload={
            "username": "alice",
            "target": {
                "kind": "repo",
                "repo": {
                    "repository": "acme/app",
                    "commit": COMMIT,
                    "draft_files": {"optimize.toml": "[run]\n"},
                    "environment": {"readonly_paths": ["vendor/lib"]},
                },
            },
        },
        result={"best_candidate": "PATCH-B", "versions": [{"candidate": "PATCH-A"}]},
    )
    seen: list[str] = []
    drafts: list[Any] = []

    def fake_zip(repository, commit, patch_text, token, workdir, **kwargs):
        """Record the call and write a stand-in zip.

        Args:
            repository: Repository name.
            commit: Pinned commit.
            patch_text: The version's patch.
            token: GitHub token.
            workdir: Folder for the zip.
            **kwargs: Host options, the run's ``drafts`` and its ``readonly_paths``.

        Returns:
            The zip path.
        """
        seen.append(patch_text)
        drafts.append((kwargs.get("drafts"), kwargs.get("readonly_paths")))
        out = workdir / "app.zip"
        out.write_bytes(b"PK")
        return out

    client = _client(store)
    with patch.object(repository_module, "patched_repo_zip", side_effect=fake_zip):
        ok = client.post("/optimizations/done-run/repository/archive", json={"patch": "PATCH-A"})
        refused = client.post("/optimizations/done-run/repository/archive", json={"patch": "rm -rf"})

    assert ok.status_code == 200, ok.text
    assert ok.headers["content-type"] == "application/zip"
    assert 'filename="app.zip"' in ok.headers["content-disposition"]
    assert refused.status_code == 404
    assert refused.json()["code"] == "optimization.repo_version_unknown"
    assert seen == ["PATCH-A"]
    assert drafts == [({"optimize.toml": "[run]\n"}, ["vendor/lib"])]


def test_archive_reports_a_failed_clone(store: FakeJobStore) -> None:
    """Turn a clone or apply failure into a 502 the page can show."""
    client = _client(store)
    with patch.object(repository_module, "patched_repo_zip", side_effect=RepoFetchError("git fetch failed: nope")):
        response = client.post("/optimizations/repo-run/repository/archive", json={"patch": ""})

    assert response.status_code == 502
    assert response.json()["code"] == "optimization.repo_archive_failed"
