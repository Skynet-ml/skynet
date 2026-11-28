"""Tests for the repository-run file routes under ``/optimizations/{id}/repository``."""

from __future__ import annotations

import base64
import hashlib
import io
import zipfile
from collections.abc import Iterator
from pathlib import Path
from typing import Any
from unittest.mock import patch
from urllib.parse import urlparse

import httpx
import pytest
from fastapi import FastAPI
from fastapi.responses import JSONResponse
from fastapi.testclient import TestClient

from ...config import settings
from ...connectors.github_repo import RepoFetchError
from ...worker import local_folder
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


def _tree_hash(folder: Path) -> str:
    """Hash every path and file's bytes under a folder.

    Args:
        folder: The folder.

    Returns:
        The hex digest.
    """
    digest = hashlib.sha256()
    for path in sorted(folder.rglob("*")):
        digest.update(path.relative_to(folder).as_posix().encode() + b"\0")
        if path.is_file():
            digest.update(path.read_bytes())
    return digest.hexdigest()


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
        return JSONResponse(
            status_code=exc.status_code, content={"detail": exc.detail, "code": exc.code, "params": exc.params}
        )

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
                    "draft_files": {"src/main.py": "drafted\n", "cfg/ascent.toml": "[run]\n"},
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
        {"path": "cfg/ascent.toml", "type": "file", "size": 6},
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


def _seed_deleting_run(store: FakeJobStore) -> None:
    """Seed a run on the same commit whose setup deleted ``src/main.py`` and wrote ``README.md/notes.txt``.

    Args:
        store: Fake job store.
    """
    store.seed_job(
        "deleting-run",
        payload={
            "username": "alice",
            "target": {
                "kind": "repo",
                "repo": {
                    "repository": "acme/app",
                    "commit": COMMIT,
                    "editable_paths": ["."],
                    "draft_files": {"README.md/notes.txt": "n\n"},
                    "draft_deleted": ["src/main.py", "README.md"],
                },
            },
        },
    )


def test_tree_and_file_routes_leave_out_the_run_deleted_files(store: FakeJobStore, github: _FakeGithub) -> None:
    """A deleted file and the folder it emptied leave the run's tree, and the file reads as missing (SKY-997).

    A deleted file may come back as a folder of draft files, as the deletes apply first.
    """
    _seed_deleting_run(store)
    client = _client(store)

    tree = client.get("/optimizations/deleting-run/repository/tree").json()
    gone = client.get("/optimizations/deleting-run/repository/file", params={"path": "src/main.py"}).json()
    plain = client.get("/optimizations/repo-run/repository/tree").json()

    assert tree["entries"] == [
        {"path": "README.md", "type": "dir", "size": None},
        {"path": "README.md/notes.txt", "type": "file", "size": 2},
    ]
    assert gone == {
        "path": "src/main.py",
        "content": None,
        "binary": False,
        "too_large": False,
        "missing": True,
        "size": None,
    }
    assert [entry["path"] for entry in plain["entries"]] == ["README.md", "src", "src/main.py"]
    assert [call[0] for call in github.calls] == [TREE_PATH]


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
                    "draft_files": {"ascent.toml": "[run]\n"},
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
    assert drafts == [({"ascent.toml": "[run]\n"}, ["vendor/lib"])]


def test_archive_reports_a_failed_clone(store: FakeJobStore) -> None:
    """Turn a clone or apply failure into a 502 the page can show."""
    client = _client(store)
    with patch.object(repository_module, "patched_repo_zip", side_effect=RepoFetchError("git fetch failed: nope")):
        response = client.post("/optimizations/repo-run/repository/archive", json={"patch": ""})

    assert response.status_code == 502
    assert response.json()["code"] == "optimization.repo_archive_failed"


def test_a_local_run_reads_its_kept_folder_copy(
    store: FakeJobStore, monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    """The tree, files and download of a local run come from the kept copy, with drafts, and no host read (SKY-990).

    Args:
        store: Fake job store.
        monkeypatch: Pytest monkeypatch.
        tmp_path: Scratch folder.
    """
    monkeypatch.setattr(settings, "local_mode", True)
    monkeypatch.setattr(settings, "local_state_dir", str(tmp_path / "state"))
    folder = tmp_path / "app"
    (folder / "src").mkdir(parents=True)
    (folder / "src" / "main.py").write_text("x = 1\n")
    workdir = tmp_path / "w"
    workdir.mkdir()
    commit = local_folder.snapshot_folder(folder, workdir).commit
    # The folder may change or go away after setup; the run reads its kept copy.
    (folder / "src" / "main.py").write_text("x = 9\n")
    patch_text = "--- a/src/main.py\n+++ b/src/main.py\n@@ -1 +1 @@\n-x = 1\n+x = 2\n"
    store.seed_job(
        "local-run",
        payload={
            "username": "alice",
            "target": {
                "kind": "repo",
                "repo": {
                    "provider": "local",
                    "repository": "local/app",
                    "path": str(folder),
                    "commit": commit,
                    "editable_paths": ["."],
                    "draft_files": {"ascent.toml": "[run]\n"},
                },
            },
        },
        result={"best_candidate": patch_text},
    )
    client = _client(store)

    with patch("core.connectors.transport.CLIENT.request", side_effect=AssertionError("no host read")):
        tree = client.get("/optimizations/local-run/repository/tree")
        found = client.get("/optimizations/local-run/repository/file", params={"path": "src/main.py"})
        missing = client.get("/optimizations/local-run/repository/file", params={"path": "src/new.py"})
        archive = client.post("/optimizations/local-run/repository/archive", json={"patch": patch_text})

    assert tree.status_code == 200, tree.text
    assert tree.json()["repository"] == "local/app"
    assert [entry["path"] for entry in tree.json()["entries"]] == ["ascent.toml", "src", "src/main.py"]
    assert found.json()["content"] == "x = 1\n"
    assert missing.json()["missing"] is True
    assert archive.status_code == 200, archive.text
    with zipfile.ZipFile(io.BytesIO(archive.content)) as zipped:
        assert zipped.read("app/src/main.py") == b"x = 2\n"
        assert zipped.read("app/ascent.toml") == b"[run]\n"


def test_a_local_run_without_its_kept_copy_is_a_502(
    store: FakeJobStore, monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    """A run opened on a computer that lacks the kept copy says so, instead of reading a host.

    Args:
        store: Fake job store.
        monkeypatch: Pytest monkeypatch.
        tmp_path: Scratch folder.
    """
    monkeypatch.setattr(settings, "local_state_dir", str(tmp_path))
    store.seed_job(
        "local-run",
        payload={
            "username": "alice",
            "target": {
                "kind": "repo",
                "repo": {
                    "provider": "local",
                    "repository": "local/app",
                    "path": "/gone",
                    "commit": COMMIT,
                    "editable_paths": ["."],
                },
            },
        },
    )

    response = _client(store).get("/optimizations/local-run/repository/tree")

    assert response.status_code == 502
    assert response.json()["code"] == "optimization.repo_archive_failed"


class _LoggingJobStore(FakeJobStore):
    """The router fake, plus the log append that Apply and Undo use to move the run page's ETag."""

    def append_log(self, optimization_id: str, **entry: Any) -> None:
        """Keep one log entry.

        Args:
            optimization_id: The run.
            **entry: The entry's fields.
        """
        self._logs.setdefault(optimization_id, []).append(entry)


_MAIN_PATCH = (
    "diff --git a/src/main.py b/src/main.py\n--- a/src/main.py\n+++ b/src/main.py\n@@ -1 +1 @@\n-x = 1\n+x = 2\n"
)


def _local_apply_run(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
    *,
    status: str = "success",
    editable: list[str] | None = None,
    files: dict[str, str] | None = None,
    patch: str = _MAIN_PATCH,
    protected: list[str] | None = None,
    drafts: dict[str, str] | None = None,
) -> tuple[_LoggingJobStore, Path]:
    """Seed a stopped local folder run whose best version changes one file.

    Args:
        monkeypatch: Pytest monkeypatch.
        tmp_path: Scratch folder.
        status: The run's status.
        editable: The run's editable paths.
        files: More files for the folder, by path.
        patch: The best version.
        protected: The run's protected paths.
        drafts: The run's setup drafts; one draft file when not given.

    Returns:
        The store and the run's folder.
    """
    monkeypatch.setattr(settings, "local_mode", True)
    monkeypatch.setattr(settings, "local_state_dir", str(tmp_path / "state"))
    folder = tmp_path / "app"
    for name, text in {"src/main.py": "x = 1\n", **(files or {})}.items():
        (folder / name).parent.mkdir(parents=True, exist_ok=True)
        (folder / name).write_text(text)
    workdir = tmp_path / "w"
    workdir.mkdir()
    commit = local_folder.snapshot_folder(folder, workdir).commit
    jobs = _LoggingJobStore()
    jobs.seed_job(
        "local-run",
        status=status,
        payload={
            "username": "alice",
            "target": {
                "kind": "repo",
                "repo": {
                    "provider": "local",
                    "repository": "local/app",
                    "path": str(folder),
                    "commit": commit,
                    "editable_paths": editable or ["src"],
                    "draft_files": {"src/cfg.toml": "[run]\n"} if drafts is None else drafts,
                    "environment": {"readonly_paths": [], "protected_paths": protected or []},
                },
            },
        },
        result={
            "best_candidate": patch,
            "details": {"kept": True},
        },
    )
    return jobs, folder


def test_apply_and_undo_change_the_folder_and_keep_the_first_apply_time(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    """Apply writes the best version and drafts, Undo restores the folder, and ``applied_at`` stays (SKY-991).

    Args:
        monkeypatch: Pytest monkeypatch.
        tmp_path: Scratch folder.
    """
    jobs, folder = _local_apply_run(monkeypatch, tmp_path)
    client = _client(jobs)

    applied = client.post("/optimizations/local-run/repository/apply")
    again = client.post("/optimizations/local-run/repository/apply")

    assert applied.status_code == 200, applied.text
    assert applied.json()["applied"] is True
    first_time = applied.json()["applied_at"]
    assert first_time
    assert (folder / "src" / "main.py").read_text() == "x = 2\n"
    assert (folder / "src" / "cfg.toml").read_text() == "[run]\n"
    assert again.status_code == 200, again.text
    assert again.json() == {"applied": True, "applied_at": first_time}
    stored = jobs.get_job("local-run")["result"]
    assert stored["details"] == {"kept": True, "local_apply": {"applied": True, "applied_at": first_time}}
    assert stored["best_candidate"].startswith("diff --git a/src/main.py")

    undone = client.post("/optimizations/local-run/repository/undo")
    reapplied = client.post("/optimizations/local-run/repository/apply")

    assert undone.status_code == 200, undone.text
    assert undone.json() == {"applied": False, "applied_at": first_time}
    assert reapplied.json() == {"applied": True, "applied_at": first_time}
    assert client.post("/optimizations/local-run/repository/undo").status_code == 200
    assert (folder / "src" / "main.py").read_text() == "x = 1\n"
    assert not (folder / "src" / "cfg.toml").exists()
    assert jobs.get_job("local-run")["result"]["details"]["local_apply"] == {"applied": False, "applied_at": first_time}
    assert jobs.get_log_count("local-run") == 4


def test_apply_with_a_changed_file_writes_nothing_and_names_it(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
    """A file the version changes that the user edited since setup stops Apply, with the file named.

    Args:
        monkeypatch: Pytest monkeypatch.
        tmp_path: Scratch folder.
    """
    jobs, folder = _local_apply_run(monkeypatch, tmp_path)
    (folder / "src" / "main.py").write_text("x = 9\n")

    response = _client(jobs).post("/optimizations/local-run/repository/apply")

    assert response.status_code == 409
    assert response.json()["code"] == "optimization.local_folder_conflict"
    assert response.json()["params"] == {"files": "src/main.py"}
    assert (folder / "src" / "main.py").read_text() == "x = 9\n"
    assert not (folder / "src" / "cfg.toml").exists()
    assert "local_apply" not in jobs.get_job("local-run")["result"]["details"]


@pytest.mark.parametrize(
    ("status", "editable", "local_mode", "code"),
    [
        ("running", None, True, "optimization.local_folder_failed"),
        ("success", ["docs"], True, "optimization.local_folder_failed"),
        ("success", None, False, "optimization.local_folder_failed"),
    ],
)
def test_apply_is_refused_while_running_off_this_computer_or_outside_the_editable_paths(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
    status: str,
    editable: list[str] | None,
    local_mode: bool,
    code: str,
) -> None:
    """A running run, a patch that breaks the editing rules, and a hosted API never write the folder.

    Args:
        monkeypatch: Pytest monkeypatch.
        tmp_path: Scratch folder.
        status: The run's status.
        editable: The run's editable paths.
        local_mode: Whether this is the desktop app.
        code: The expected error code.
    """
    jobs, folder = _local_apply_run(monkeypatch, tmp_path, status=status, editable=editable)
    monkeypatch.setattr(settings, "local_mode", local_mode)

    response = _client(jobs).post("/optimizations/local-run/repository/apply")

    assert response.status_code == 409, response.text
    assert response.json()["code"] == code
    assert (folder / "src" / "main.py").read_text() == "x = 1\n"


def test_apply_and_undo_are_only_for_a_local_run_and_its_editors(
    store: FakeJobStore, monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    """A hosted repository run has no folder (404), and a stranger cannot see the local run (404).

    Args:
        store: Fake job store.
        monkeypatch: Pytest monkeypatch.
        tmp_path: Scratch folder.
    """
    monkeypatch.setattr(settings, "local_mode", True)
    jobs, folder = _local_apply_run(monkeypatch, tmp_path)
    stranger = AuthenticatedUser(username="mallory", role="user", groups=())

    hosted = _client(store).post("/optimizations/repo-run/repository/apply")
    hidden = _client(jobs, stranger).post("/optimizations/local-run/repository/apply")
    nothing = _client(jobs).post("/optimizations/local-run/repository/undo")

    assert hosted.status_code == 404
    assert hidden.status_code == 404
    # Nothing is applied, so Undo has nothing to do, and no Apply time appears.
    assert nothing.status_code == 200, nothing.text
    assert nothing.json() == {"applied": False, "applied_at": None}
    assert (folder / "src" / "main.py").read_text() == "x = 1\n"


def _edit(path: str, old: str, new: str) -> str:
    """Return a git-style patch that changes one line of one file.

    Args:
        path: The file the patch names.
        old: The line before.
        new: The line after.

    Returns:
        The patch.
    """
    return f"diff --git a/{path} b/{path}\n--- a/{path}\n+++ b/{path}\n@@ -1 +1 @@\n-{old}\n+{new}\n"


@pytest.mark.parametrize(
    ("files", "patch", "protected"),
    [
        ({"src/eval/metric.py": "score = 1\n"}, _edit("src/EVAL/metric.py", "score = 1", "score = 9"), ["src/eval"]),
        ({"src/eval/metric.py": "score = 1\n"}, _edit("src/eval/metric.py", "score = 1", "score = 9"), ["SRC/Eval"]),
        (
            {
                "ascent.toml": 'version = 1\n[run]\nentry = "src/main.py:f"\n[metric]\nexploits = "src/attacks"\n',
                "src/attacks/a.py": "attack = 1\n",
            },
            _edit("src/Attacks/a.py", "attack = 1", "attack = 0"),
            [],
        ),
    ],
)
def test_apply_refuses_a_case_variant_of_a_protected_or_hidden_path(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path, files: dict[str, str], patch: str, protected: list[str]
) -> None:
    """A protected path or the custom exploit folder in another case never reaches the folder.

    Args:
        monkeypatch: Pytest monkeypatch.
        tmp_path: Scratch folder.
        files: The folder's files besides ``src/main.py``.
        patch: The best version.
        protected: The run's protected paths.
    """
    jobs, folder = _local_apply_run(monkeypatch, tmp_path, files=files, patch=patch, protected=protected)

    response = _client(jobs).post("/optimizations/local-run/repository/apply")

    assert response.status_code == 409, response.text
    assert response.json()["code"] == "optimization.local_folder_failed"
    for name, text in files.items():
        assert (folder / name).read_text() == text
    assert not (folder / "src" / "cfg.toml").exists()
    assert not local_folder.is_applied("local-run")


def test_a_lost_state_save_after_apply_still_leads_to_a_working_undo(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    """The Undo record is the one source of the applied state, so a failed row save heals on the next Apply.

    Args:
        monkeypatch: Pytest monkeypatch.
        tmp_path: Scratch folder.
    """
    jobs, folder = _local_apply_run(monkeypatch, tmp_path)
    real_update = jobs.update_job
    calls = {"n": 0}

    def fail_once(optimization_id: str, **fields: Any) -> None:
        """Fail the first save of the run's state.

        Args:
            optimization_id: The run.
            **fields: The fields to save.

        Raises:
            RuntimeError: On the first call.
        """
        calls["n"] += 1
        if calls["n"] == 1:
            raise RuntimeError("database is gone")
        real_update(optimization_id, **fields)

    monkeypatch.setattr(jobs, "update_job", fail_once)
    client = _client(jobs)
    lost = client.post("/optimizations/local-run/repository/apply")
    assert lost.status_code == 500
    assert (folder / "src" / "main.py").read_text() == "x = 2\n"
    assert "local_apply" not in jobs.get_job("local-run")["result"]["details"]

    repaired = client.post("/optimizations/local-run/repository/apply")
    undone = client.post("/optimizations/local-run/repository/undo")

    assert repaired.status_code == 200, repaired.text
    assert repaired.json()["applied"] is True
    assert undone.status_code == 200, undone.text
    assert undone.json() == {"applied": False, "applied_at": repaired.json()["applied_at"]}
    assert (folder / "src" / "main.py").read_text() == "x = 1\n"
    assert not (folder / "src" / "cfg.toml").exists()


def test_a_lost_state_save_after_undo_still_leads_to_a_working_undo(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    """A failed row save after a good Undo keeps the Undo record, so a repeat Undo saves the row.

    Args:
        monkeypatch: Pytest monkeypatch.
        tmp_path: Scratch folder.
    """
    jobs, folder = _local_apply_run(monkeypatch, tmp_path)
    original = _tree_hash(folder)
    client = _client(jobs)
    applied = client.post("/optimizations/local-run/repository/apply")
    assert applied.status_code == 200, applied.text
    real_update = jobs.update_job
    calls = {"n": 0}

    def fail_once(optimization_id: str, **fields: Any) -> None:
        """Fail the first save of the run's state during Undo.

        Args:
            optimization_id: The run.
            **fields: The fields to save.

        Raises:
            RuntimeError: On the first call.
        """
        calls["n"] += 1
        if calls["n"] == 1:
            raise RuntimeError("database is gone")
        real_update(optimization_id, **fields)

    monkeypatch.setattr(jobs, "update_job", fail_once)
    lost = client.post("/optimizations/local-run/repository/undo")
    assert lost.status_code == 500
    assert jobs.get_job("local-run")["result"]["details"]["local_apply"]["applied"] is True

    undone = client.post("/optimizations/local-run/repository/undo")

    assert undone.status_code == 200, undone.text
    assert undone.json() == {"applied": False, "applied_at": applied.json()["applied_at"]}
    assert jobs.get_job("local-run")["result"]["details"]["local_apply"]["applied"] is False
    assert _tree_hash(folder) == original
    assert not local_folder.is_applied("local-run")


def test_a_failed_record_delete_after_undo_does_not_block_the_next_apply(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    """A record left by a saved Undo, with every file back, does not stop Apply, and Undo after it works.

    Args:
        monkeypatch: Pytest monkeypatch.
        tmp_path: Scratch folder.
    """
    jobs, folder = _local_apply_run(monkeypatch, tmp_path)
    original = _tree_hash(folder)
    client = _client(jobs)
    assert client.post("/optimizations/local-run/repository/apply").status_code == 200
    real_forget = repository_module.forget_apply
    calls = {"n": 0}

    def fail_once(run_id: str) -> None:
        """Fail the first record delete.

        Args:
            run_id: The run.

        Raises:
            OSError: On the first call.
        """
        calls["n"] += 1
        if calls["n"] == 1:
            raise OSError("disk is gone")
        real_forget(run_id)

    monkeypatch.setattr(repository_module, "forget_apply", fail_once)
    lost = client.post("/optimizations/local-run/repository/undo")
    assert lost.status_code == 500
    assert jobs.get_job("local-run")["result"]["details"]["local_apply"]["applied"] is False
    assert local_folder.is_applied("local-run")

    applied = client.post("/optimizations/local-run/repository/apply")

    assert applied.status_code == 200, applied.text
    assert applied.json()["applied"] is True
    assert (folder / "src" / "main.py").read_text() == "x = 2\n"
    undone = client.post("/optimizations/local-run/repository/undo")
    assert undone.status_code == 200, undone.text
    assert undone.json()["applied"] is False
    assert _tree_hash(folder) == original
    assert not local_folder.is_applied("local-run")


def test_a_repeat_apply_after_a_failed_rollback_does_not_report_applied(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    """A kept Undo record with a half-applied folder makes a repeat Apply a 409, the row asks for Undo, and Undo works.

    Args:
        monkeypatch: Pytest monkeypatch.
        tmp_path: Scratch folder.
    """
    jobs, folder = _local_apply_run(monkeypatch, tmp_path)
    real_put = local_folder._put
    calls = {"n": 0}

    def flaky_put(*args: object, **kwargs: object) -> None:
        """Fail every write after the first, as a full disk does.

        Args:
            *args: Passed to ``_put``.
            **kwargs: Passed to ``_put``.

        Raises:
            OSError: From the second call on.
        """
        calls["n"] += 1
        if calls["n"] >= 2:
            raise OSError(28, "No space left on device")
        real_put(*args, **kwargs)

    monkeypatch.setattr(local_folder, "_put", flaky_put)
    client = _client(jobs)
    failed = client.post("/optimizations/local-run/repository/apply")
    monkeypatch.setattr(local_folder, "_put", real_put)

    undo_needed = {"applied": False, "applied_at": None, "undo_needed": True}
    assert failed.status_code == 409, failed.text
    assert local_folder.is_applied("local-run")
    assert (folder / "src" / "main.py").read_text() == "x = 1\n"
    # The page reads this state and offers Undo, not only Apply.
    assert jobs.get_job("local-run")["result"]["details"]["local_apply"] == undo_needed

    again = client.post("/optimizations/local-run/repository/apply")

    assert again.status_code == 409, again.text
    assert again.json()["code"] == "optimization.local_folder_failed"
    assert jobs.get_job("local-run")["result"]["details"]["local_apply"] == undo_needed

    undone = client.post("/optimizations/local-run/repository/undo")

    assert undone.status_code == 200, undone.text
    assert undone.json() == {"applied": False, "applied_at": None}
    assert jobs.get_job("local-run")["result"]["details"]["local_apply"] == {"applied": False, "applied_at": None}
    assert (folder / "src" / "main.py").read_text() == "x = 1\n"
    assert not (folder / "src" / "cfg.toml").exists()


def test_a_record_that_cannot_be_deleted_gives_a_409_not_a_500(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
    """A record delete the disk refuses is a 409 with its reason, after Undo and on the next Apply.

    Args:
        monkeypatch: Pytest monkeypatch.
        tmp_path: Scratch folder.
    """
    jobs, folder = _local_apply_run(monkeypatch, tmp_path)
    client = _client(jobs)
    assert client.post("/optimizations/local-run/repository/apply").status_code == 200
    record = local_folder.applied_record("local-run")
    real_unlink = Path.unlink

    def refuse_record(self: Path, missing_ok: bool = False) -> None:
        """Refuse to delete the Undo record, as a read-only state folder does.

        Args:
            self: The path to delete.
            missing_ok: Passed to ``Path.unlink``.

        Raises:
            PermissionError: For the Undo record.
        """
        if self == record:
            raise PermissionError(13, "Permission denied")
        real_unlink(self, missing_ok=missing_ok)

    monkeypatch.setattr(Path, "unlink", refuse_record)
    undone = client.post("/optimizations/local-run/repository/undo")
    applied = client.post("/optimizations/local-run/repository/apply")

    assert undone.status_code == 409, undone.text
    assert undone.json()["code"] == "optimization.local_folder_failed"
    assert "Undo record could not be deleted" in undone.json()["params"]["reason"]
    assert applied.status_code == 409, applied.text
    assert applied.json()["code"] == "optimization.local_folder_failed"
    assert (folder / "src" / "main.py").read_text() == "x = 1\n"


def test_apply_without_a_local_state_folder_is_a_409(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
    """A computer with no local state folder cannot read the kept copy, so Apply refuses with a 409, not a 500.

    Args:
        monkeypatch: Pytest monkeypatch.
        tmp_path: Scratch folder.
    """
    jobs, folder = _local_apply_run(monkeypatch, tmp_path)
    monkeypatch.setattr(settings, "local_state_dir", "")

    response = _client(jobs).post("/optimizations/local-run/repository/apply")

    assert response.status_code == 409, response.text
    assert response.json()["code"] == "optimization.local_folder_failed"
    assert (folder / "src" / "main.py").read_text() == "x = 1\n"


def test_apply_of_a_baseline_best_writes_the_setup_drafts(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
    """A best version equal to the start still applies the setup drafts, and Undo takes them back.

    Args:
        monkeypatch: Pytest monkeypatch.
        tmp_path: Scratch folder.
    """
    jobs, folder = _local_apply_run(monkeypatch, tmp_path, patch="")
    client = _client(jobs)

    applied = client.post("/optimizations/local-run/repository/apply")

    assert applied.status_code == 200, applied.text
    assert applied.json()["applied"] is True
    assert (folder / "src" / "main.py").read_text() == "x = 1\n"
    assert (folder / "src" / "cfg.toml").read_text() == "[run]\n"
    assert client.post("/optimizations/local-run/repository/undo").status_code == 200
    assert not (folder / "src" / "cfg.toml").exists()


def test_apply_of_a_baseline_best_without_drafts_is_a_409(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
    """With no patch and no setup drafts there is nothing to write, so Apply refuses.

    Args:
        monkeypatch: Pytest monkeypatch.
        tmp_path: Scratch folder.
    """
    jobs, folder = _local_apply_run(monkeypatch, tmp_path, patch="", drafts={})

    response = _client(jobs).post("/optimizations/local-run/repository/apply")

    assert response.status_code == 409
    assert response.json()["code"] == "optimization.no_result_pending"
    assert (folder / "src" / "main.py").read_text() == "x = 1\n"
    assert "local_apply" not in jobs.get_job("local-run")["result"]["details"]


def test_undo_with_a_lost_undo_record_is_a_409_and_keeps_the_run_applied(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    """An applied run whose Undo record is gone refuses Undo, so the page does not claim the folder is back.

    Args:
        monkeypatch: Pytest monkeypatch.
        tmp_path: Scratch folder.
    """
    jobs, folder = _local_apply_run(monkeypatch, tmp_path)
    client = _client(jobs)
    applied = client.post("/optimizations/local-run/repository/apply").json()
    local_folder.applied_record("local-run").unlink()

    response = client.post("/optimizations/local-run/repository/undo")

    assert response.status_code == 409
    assert response.json()["code"] == "optimization.local_folder_undo_missing"
    assert jobs.get_job("local-run")["result"]["details"]["local_apply"] == applied
    assert (folder / "src" / "main.py").read_text() == "x = 2\n"


def test_undo_without_a_local_state_folder_is_a_409(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
    """A computer with no local state folder cannot read the Undo record, so Undo refuses with a 409, not a 500.

    Args:
        monkeypatch: Pytest monkeypatch.
        tmp_path: Scratch folder.
    """
    jobs, folder = _local_apply_run(monkeypatch, tmp_path)
    monkeypatch.setattr(settings, "local_state_dir", "")

    response = _client(jobs).post("/optimizations/local-run/repository/undo")

    assert response.status_code == 409, response.text
    assert response.json()["code"] == "optimization.local_folder_failed"
    assert (folder / "src" / "main.py").read_text() == "x = 1\n"


def test_undo_on_a_run_never_applied_saves_and_logs_nothing(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
    """An Undo before any Apply is a no-op 200: no ``local_apply`` state and no log line.

    Args:
        monkeypatch: Pytest monkeypatch.
        tmp_path: Scratch folder.
    """
    jobs, folder = _local_apply_run(monkeypatch, tmp_path)

    response = _client(jobs).post("/optimizations/local-run/repository/undo")

    assert response.status_code == 200, response.text
    assert response.json() == {"applied": False, "applied_at": None}
    assert "local_apply" not in jobs.get_job("local-run")["result"]["details"]
    assert jobs.get_log_count("local-run") == 0
    assert (folder / "src" / "main.py").read_text() == "x = 1\n"
