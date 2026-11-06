"""Tests for creating a user's repository and pushing candidate branches to it."""

from __future__ import annotations

import re
import subprocess
from pathlib import Path
from typing import Any

import httpx
import pytest

from ...api.errors import DomainError
from .. import github_create_repo
from ..github_create_repo import (
    RepoCreateError,
    create_repository_with_files,
    existing_repository,
    push_initial_commit,
)
from ..github_publish import BEST_BRANCH, RepoPublishError, push_candidates

_GIT = ["git", "-c", "user.name=t", "-c", "user.email=t@t", "-c", "commit.gpgsign=false"]
_FILES = {"optimize.toml": "[paths]\neditable = ['program/']\n", "program/pipeline.py": "PROMPT = 'a'\n"}


def _git(cwd: Path, *arguments: str) -> str:
    """Run git in a fixture repository.

    Args:
        cwd: Repository root.
        *arguments: Arguments after ``git``.

    Returns:
        Standard output, stripped.
    """
    return subprocess.run([*_GIT, *arguments], cwd=cwd, check=True, capture_output=True, text=True).stdout.strip()


def _bare(root: Path) -> str:
    """Create an empty bare repository that stands in for the new GitHub repository.

    Args:
        root: Folder to create.

    Returns:
        Its ``file://`` URL.
    """
    root.mkdir(parents=True)
    _git(root, "init", "--quiet", "--bare", "-b", "main")
    _git(root, "config", "uploadpack.allowAnySHA1InWant", "true")
    return f"file://{root}"


def _github(monkeypatch: pytest.MonkeyPatch, *, taken: bool | None = None) -> list[dict[str, Any]]:
    """Stand in for GitHub's create-repository, user and repository endpoints.

    Args:
        monkeypatch: Pytest fixture.
        taken: ``None`` to accept the create; otherwise refuse it with a 422,
            and answer the repository lookup as taken or not.

    Returns:
        The create bodies sent, in order.
    """
    sent: list[dict[str, Any]] = []

    def fake_request(method: str, url: str, *, json: dict[str, Any], **_: Any) -> httpx.Response:
        assert (method, url) == ("POST", "https://api.github.com/user/repos")
        sent.append(json)
        if taken is not None:
            raise DomainError("connectors.provider_error", status=502, provider="GitHub", status_code=422)
        return httpx.Response(201, json={"full_name": f"ada/{json['name']}", "default_branch": "main"})

    def fake_get_json(url: str, **_: Any) -> dict[str, Any]:
        if url == "https://api.github.com/user":
            return {"login": "ada"}
        assert url == "https://api.github.com/repos/ada/starter"
        if not taken:
            raise DomainError("connectors.not_found", status=404, provider="GitHub")
        return {"full_name": "ada/starter", "default_branch": "trunk"}

    monkeypatch.setattr(github_create_repo, "request", fake_request)
    monkeypatch.setattr(github_create_repo, "get_json", fake_get_json)
    return sent


def test_private_repository_gets_the_files_as_its_first_commit(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
    """A private repository is created empty, then the files land as one commit on its default branch."""
    sent = _github(monkeypatch)
    origin = tmp_path / "origin"

    created = create_repository_with_files("tok", "starter", _FILES, private=True, remote=_bare(origin))

    assert sent == [{"name": "starter", "private": True, "auto_init": False}]
    assert (created.full_name, created.default_branch) == ("ada/starter", "main")
    assert re.fullmatch(r"[0-9a-f]{40}", created.commit)
    assert _git(origin, "rev-parse", "main") == created.commit
    assert _git(origin, "show", f"{created.commit}:program/pipeline.py") == "PROMPT = 'a'"
    assert _git(origin, "rev-list", "--count", "main") == "1"


def test_public_repository_sends_its_description(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
    """A public repository asks GitHub for visibility to all, with the description."""
    sent = _github(monkeypatch)

    create_repository_with_files(
        "tok", "starter", _FILES, private=False, description="My task", remote=_bare(tmp_path / "o")
    )

    assert sent == [{"name": "starter", "private": False, "auto_init": False, "description": "My task"}]


def test_taken_name_fails_in_plain_words(monkeypatch: pytest.MonkeyPatch) -> None:
    """A 422 for a name the account already uses says so."""
    _github(monkeypatch, taken=True)

    with pytest.raises(RepoCreateError, match="already has a repository named 'starter'"):
        create_repository_with_files("tok", "starter", _FILES, private=True)


def test_invalid_name_is_not_reported_as_taken(monkeypatch: pytest.MonkeyPatch) -> None:
    """A 422 for a free name means GitHub does not accept the name."""
    _github(monkeypatch, taken=False)

    with pytest.raises(RepoCreateError, match="does not accept 'starter'"):
        create_repository_with_files("tok", "starter", _FILES, private=True)


@pytest.mark.parametrize(("taken", "expected"), [(True, ("ada/starter", "trunk")), (False, None)])
def test_existing_repository_lookup(
    monkeypatch: pytest.MonkeyPatch, taken: bool, expected: tuple[str, str] | None
) -> None:
    """The lookup returns the repository and its default branch, or ``None`` when the name is free."""
    _github(monkeypatch, taken=taken)

    assert existing_repository("tok", "starter") == expected


def test_existing_repository_lookup_raises_when_github_fails(monkeypatch: pytest.MonkeyPatch) -> None:
    """A GitHub outage raises instead of reporting the name as free."""

    def fake_get_json(url: str, **_: Any) -> dict[str, Any]:
        if url == "https://api.github.com/user":
            return {"login": "ada"}
        raise DomainError("connectors.provider_error", status=502, provider="GitHub", status_code=500)

    monkeypatch.setattr(github_create_repo, "get_json", fake_get_json)

    with pytest.raises(RepoCreateError, match="Could not check GitHub"):
        existing_repository("tok", "starter")


@pytest.mark.parametrize("files", [{}, {"../escape.py": "x"}, {"/abs.py": "x"}, {".git/config": "x"}])
def test_bad_files_fail_before_github_is_called(monkeypatch: pytest.MonkeyPatch, files: dict[str, str]) -> None:
    """No repository is created when the first commit could not be pushed."""
    sent = _github(monkeypatch)

    with pytest.raises(RepoCreateError):
        create_repository_with_files("tok", "starter", files, private=True)

    assert sent == []


def test_initial_commit_retry_returns_the_same_commit(tmp_path: Path) -> None:
    """A retried first push finds its own commit; other content on the branch is refused."""
    remote = _bare(tmp_path / "origin")

    first = push_initial_commit("ada/starter", "main", _FILES, "Initial commit", None, remote=remote)
    again = push_initial_commit("ada/starter", "main", _FILES, "Initial commit", None, remote=remote)

    assert again == first
    with pytest.raises(RepoCreateError, match="already has other content"):
        push_initial_commit("ada/starter", "main", {"other.py": "x"}, "Initial commit", None, remote=remote)


def _patch(origin: Path, base: str, content: str) -> str:
    """Make one version: ``program/pipeline.py`` with new content, against ``base``.

    Args:
        origin: Bare repository.
        base: The pinned commit.
        content: New file content.

    Returns:
        ``git diff --binary`` output.
    """
    scratch = origin.parent / f"scratch-{abs(hash(content))}"
    _git(origin.parent, "clone", "--quiet", str(origin), str(scratch))
    _git(scratch, "checkout", "--quiet", base)
    (scratch / "program/pipeline.py").write_text(content)
    _git(scratch, "add", "--all")
    return _git(scratch, "diff", "--cached", "--binary", "HEAD") + "\n"


def _seeded(tmp_path: Path) -> tuple[Path, str, str]:
    """Create a repository holding the starter files.

    Args:
        tmp_path: Pytest temporary folder.

    Returns:
        The bare repository, its URL and the pinned first commit.
    """
    origin = tmp_path / "origin"
    remote = _bare(origin)
    return origin, remote, push_initial_commit("ada/starter", "main", _FILES, "Initial commit", None, remote=remote)


def test_each_candidate_gets_a_branch_and_best_points_at_one(tmp_path: Path) -> None:
    """Every version is one commit on the pinned commit; an empty patch is the pinned commit itself."""
    origin, remote, base = _seeded(tmp_path)
    versions = [("v1", _patch(origin, base, "PROMPT = 'b'\n")), ("v2", _patch(origin, base, "PROMPT = 'c'\n"))]

    pushed = push_candidates("ada/starter", base, [*versions, ("v0", "")], "v2", None, remote=remote)

    for version_id, prompt in (("v1", "PROMPT = 'b'"), ("v2", "PROMPT = 'c'")):
        head = _git(origin, "rev-parse", f"skynet/candidate-{version_id}")
        assert head == pushed[f"skynet/candidate-{version_id}"]
        assert _git(origin, "rev-parse", f"{head}^") == base
        assert _git(origin, "show", f"{head}:program/pipeline.py") == prompt
    assert _git(origin, "rev-parse", "skynet/candidate-v0") == base
    assert _git(origin, "rev-parse", BEST_BRANCH) == pushed["skynet/candidate-v2"]


def test_retry_is_idempotent_and_best_moves(tmp_path: Path) -> None:
    """The same versions make the same commits again, and only the best branch moves."""
    origin, remote, base = _seeded(tmp_path)
    versions = [("v1", _patch(origin, base, "PROMPT = 'b'\n")), ("v2", _patch(origin, base, "PROMPT = 'c'\n"))]

    first = push_candidates("ada/starter", base, versions, "v2", None, remote=remote)
    again = push_candidates("ada/starter", base, versions, "v2", None, remote=remote)
    moved = push_candidates("ada/starter", base, versions, "v1", None, remote=remote)

    assert again == first
    assert moved["skynet/candidate-v1"] == first["skynet/candidate-v1"]
    assert _git(origin, "rev-parse", BEST_BRANCH) == first["skynet/candidate-v1"]


@pytest.mark.parametrize(
    ("versions", "best", "message"),
    [
        ([("v1", "")], "v9", "not one of the candidates"),
        ([("v1", ""), ("v1", "")], "v1", "same id"),
        ([("bad id", "")], "bad id", "cannot be used in a branch name"),
        ([("v1", "diff --git a/x b/x\n--- a/x\n+++ b/x\n@@ -1 +1 @@\n-a\n+b\n")], "v1", "does not apply"),
    ],
)
def test_bad_candidates_push_nothing(tmp_path: Path, versions: list[tuple[str, str]], best: str, message: str) -> None:
    """A bad id, a missing best version or a stale patch fails before any branch is pushed."""
    origin, remote, base = _seeded(tmp_path)

    with pytest.raises(RepoPublishError, match=message):
        push_candidates("ada/starter", base, versions, best, None, remote=remote)

    assert _git(origin, "branch", "--list") == "* main"
