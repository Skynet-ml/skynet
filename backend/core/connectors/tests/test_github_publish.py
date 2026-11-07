"""Tests for pushing a run's best version and opening its pull request."""

from __future__ import annotations

import subprocess
from pathlib import Path
from typing import Any

import httpx
import pytest

from ...api.errors import DomainError
from ...brand import APP_NAME
from .. import github_publish
from ..github_publish import HardeningSummary, RepoPublishError, open_pull_request, push_version

_GIT = ["git", "-c", "user.name=t", "-c", "user.email=t@t", "-c", "commit.gpgsign=false"]


def _git(cwd: Path, *arguments: str) -> str:
    """Run git in a fixture repository.

    Args:
        cwd: Repository root.
        *arguments: Arguments after ``git``.

    Returns:
        Standard output, stripped.
    """
    return subprocess.run([*_GIT, *arguments], cwd=cwd, check=True, capture_output=True, text=True).stdout.strip()


def _repository(root: Path) -> str:
    """Create a repository with one commit and a later one on top.

    Args:
        root: Folder to create.

    Returns:
        The first commit, which the run pinned.
    """
    root.mkdir(parents=True)
    _git(root, "init", "--quiet", "-b", "main")
    _git(root, "config", "uploadpack.allowAnySHA1InWant", "true")
    (root / "src").mkdir()
    (root / "src/app.py").write_text("x = 1\n")
    _git(root, "add", "--all")
    _git(root, "commit", "--quiet", "-m", "init")
    pinned = _git(root, "rev-parse", "HEAD")
    (root / "README.md").write_text("later\n")
    _git(root, "add", "--all")
    _git(root, "commit", "--quiet", "-m", "later")
    return pinned


def _patch(root: Path, pinned: str) -> str:
    """Make the run's version: one edited file and one new one, against the pinned commit.

    Args:
        root: Fixture repository.
        pinned: The commit the run started from.

    Returns:
        ``git diff --binary`` output.
    """
    scratch = root.parent / "scratch"
    _git(root.parent, "clone", "--quiet", str(root), str(scratch))
    _git(scratch, "checkout", "--quiet", pinned)
    (scratch / "src/app.py").write_text("x = 2\n")
    (scratch / "src/new.py").write_text("y = 3\n")
    _git(scratch, "add", "--all")
    return _git(scratch, "diff", "--cached", "--binary", "HEAD") + "\n"


def test_push_creates_one_commit_on_the_pinned_commit(tmp_path: Path) -> None:
    """The branch holds the version as a single commit whose parent is the pinned commit."""
    origin = tmp_path / "origin"
    pinned = _repository(origin)
    patch = _patch(origin, pinned)

    push_version("acme/app", pinned, patch, "skynet/optimize-1", "Apply it", None, remote=f"file://{origin}")

    head = _git(origin, "rev-parse", "skynet/optimize-1")
    assert _git(origin, "rev-parse", f"{head}^") == pinned
    assert _git(origin, "show", f"{head}:src/app.py") == "x = 2"
    assert _git(origin, "show", f"{head}:src/new.py") == "y = 3"
    assert _git(origin, "log", "-1", "--format=%an %s", head) == f"{APP_NAME} Apply it"


def test_push_refuses_a_version_that_does_not_apply(tmp_path: Path) -> None:
    """A stale patch fails in plain words and pushes nothing."""
    origin = tmp_path / "origin"
    pinned = _repository(origin)
    stale = "diff --git a/src/app.py b/src/app.py\n--- a/src/app.py\n+++ b/src/app.py\n@@ -1 +1 @@\n-x = 9\n+x = 2\n"

    with pytest.raises(RepoPublishError, match="no longer applies"):
        push_version("acme/app", pinned, stale, "skynet/optimize-1", "Apply it", None, remote=f"file://{origin}")

    assert "skynet/optimize-1" not in _git(origin, "branch", "--list")


def _github(monkeypatch: pytest.MonkeyPatch, refuse_draft: bool = False) -> list[dict[str, Any]]:
    """Stand in for GitHub's repository and pull request endpoints.

    Args:
        monkeypatch: Pytest fixture.
        refuse_draft: Answer a draft request the way a free private repository does.

    Returns:
        The pull request bodies sent, in order.
    """
    sent: list[dict[str, Any]] = []

    def fake_request(method: str, url: str, *, json: dict[str, Any], **_: Any) -> httpx.Response:
        assert (method, url) == ("POST", "https://api.github.com/repos/acme/app/pulls")
        sent.append(json)
        if refuse_draft and json.get("draft"):
            raise DomainError("connectors.provider_error", status=502, provider="GitHub", status_code=422)
        body = {"html_url": "https://github.com/acme/app/pull/7", "number": 7, "draft": bool(json.get("draft"))}
        return httpx.Response(201, json=body)

    monkeypatch.setattr(github_publish, "request", fake_request)
    monkeypatch.setattr(github_publish, "get_json", lambda *_a, **_k: {"default_branch": "trunk"})
    return sent


def test_pull_request_opens_as_a_draft_against_the_default_branch(monkeypatch: pytest.MonkeyPatch) -> None:
    """With no branch named, the draft targets the repository's default branch."""
    sent = _github(monkeypatch)

    change = open_pull_request("tok", "acme/app", head="skynet/x", base_branch=None, title="T", body="B")

    assert sent == [{"title": "T", "head": "skynet/x", "base": "trunk", "body": "B", "draft": True}]
    assert (change.url, change.number, change.draft) == ("https://github.com/acme/app/pull/7", 7, True)


def test_pull_request_falls_back_when_drafts_are_unavailable(monkeypatch: pytest.MonkeyPatch) -> None:
    """A repository without drafts gets a regular pull request instead."""
    sent = _github(monkeypatch, refuse_draft=True)

    change = open_pull_request("tok", "acme/app", head="skynet/x", base_branch="main", title="T", body="B")

    assert [body.get("draft") for body in sent] == [True, None]
    assert sent[1]["base"] == "main"
    assert change.draft is False


def test_pull_request_reports_a_finished_hardening_job(monkeypatch: pytest.MonkeyPatch) -> None:
    """A run with a finished hardening job adds the attack and fix counts to the body."""
    sent = _github(monkeypatch)
    hardening = HardeningSummary(attacks=12, exploits_fixed=3)

    open_pull_request("tok", "acme/app", head="skynet/x", base_branch="main", title="T", body="B", hardening=hardening)

    assert sent[0]["body"] == "B\n\nMetric survived 12 attacks, 3 exploits fixed"


def test_hardening_line_survives_the_draft_fallback(monkeypatch: pytest.MonkeyPatch) -> None:
    """The regular pull request opened after a refused draft keeps the hardening line."""
    sent = _github(monkeypatch, refuse_draft=True)
    hardening = HardeningSummary(attacks=1, exploits_fixed=1)

    open_pull_request("tok", "acme/app", head="skynet/x", base_branch="main", title="T", body="", hardening=hardening)

    assert [body["body"] for body in sent] == ["Metric survived 1 attack, 1 exploit fixed"] * 2


def test_pull_request_without_hardening_keeps_the_body(monkeypatch: pytest.MonkeyPatch) -> None:
    """A run with no hardening job publishes the body exactly as given."""
    sent = _github(monkeypatch)

    open_pull_request("tok", "acme/app", head="skynet/x", base_branch="main", title="T", body="B\n", hardening=None)

    assert sent[0]["body"] == "B\n"
