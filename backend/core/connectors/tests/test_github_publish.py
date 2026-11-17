"""Tests for pushing a run's best version and opening its pull request."""

from __future__ import annotations

import subprocess
from collections.abc import Iterator
from pathlib import Path
from typing import Any

import httpx
import pytest
from sqlalchemy import create_engine
from sqlalchemy.orm import Session

from ...api.errors import DomainError
from ...brand import APP_NAME
from ...storage.effect_store import EffectStore, StepEffects
from ...storage.models import Base, JobModel
from ...storage.object_store import LocalObjectStore
from ...storage.step_log_store import StepLogStore
from .. import github_publish
from ..github_publish import (
    BEST_BRANCH,
    BranchConflictError,
    HardeningSummary,
    PublishedChange,
    RepoPublishError,
    open_pull_request,
    push_candidates,
    push_version,
)

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


def test_push_commits_the_draft_files_under_the_version(tmp_path: Path) -> None:
    """The run started from the drafted tree, so the branch holds the drafts and the version on top of them."""
    origin = tmp_path / "origin"
    pinned = _repository(origin)
    patch = _patch(origin, pinned)
    drafts = {"src/settings.py": "debug = False\n", "optimize.toml": "version = 1\n"}

    push_version(
        "acme/app", pinned, patch, "skynet/optimize-1", "Apply it", None, remote=f"file://{origin}", drafts=drafts
    )

    head = _git(origin, "rev-parse", "skynet/optimize-1")
    assert _git(origin, "rev-parse", f"{head}^") == pinned
    assert _git(origin, "show", f"{head}:src/app.py") == "x = 2"
    assert _git(origin, "show", f"{head}:src/settings.py") == "debug = False"
    assert _git(origin, "show", f"{head}:optimize.toml") == "version = 1"


def test_push_refuses_a_draft_path_that_leaves_the_repository(tmp_path: Path) -> None:
    """A stored draft path is checked again, and a bad one pushes nothing."""
    origin = tmp_path / "origin"
    pinned = _repository(origin)

    with pytest.raises(RepoPublishError):
        push_version(
            "acme/app",
            pinned,
            "",
            "skynet/optimize-1",
            "Apply it",
            None,
            remote=f"file://{origin}",
            drafts={"../escape.py": "x"},
        )

    assert "skynet/optimize-1" not in _git(origin, "branch", "--list")
    assert not (tmp_path / "escape.py").exists()


@pytest.mark.parametrize("name", [":note.md", "src/a*.py"])
def test_push_adds_a_draft_path_as_a_literal_name_never_a_pattern(tmp_path: Path, name: str) -> None:
    """A draft named like pathspec magic or a glob is added as that one file, and nothing else is added."""
    origin = tmp_path / "origin"
    pinned = _repository(origin)
    patch = _patch(origin, pinned)

    push_version(
        "acme/app",
        pinned,
        patch,
        "skynet/optimize-1",
        "Apply it",
        None,
        remote=f"file://{origin}",
        drafts={name: "z\n"},
    )

    head = _git(origin, "rev-parse", "skynet/optimize-1")
    assert _git(origin, "show", f"{head}:{name}") == "z"
    assert sorted(_git(origin, "diff", "--name-only", pinned, head).splitlines()) == sorted(
        [name, "src/app.py", "src/new.py"]
    )


def test_push_commits_a_draft_path_that_the_gitignore_ignores(tmp_path: Path) -> None:
    """The user chose each draft, so an ignored draft path is still committed on the branch."""
    origin = tmp_path / "origin"
    origin.mkdir()
    _git(origin, "init", "--quiet", "-b", "main")
    _git(origin, "config", "uploadpack.allowAnySHA1InWant", "true")
    (origin / ".gitignore").write_text("data/\n.env\n")
    (origin / "app.py").write_text("x = 1\n")
    _git(origin, "add", "--all")
    _git(origin, "commit", "--quiet", "-m", "init")
    pinned = _git(origin, "rev-parse", "HEAD")
    patch = "--- a/app.py\n+++ b/app.py\n@@ -1 +1 @@\n-x = 1\n+x = 2\n"

    push_version(
        "acme/app",
        pinned,
        patch,
        "skynet/optimize-1",
        "Apply it",
        None,
        remote=f"file://{origin}",
        drafts={"data/train.jsonl": "{}\n", ".env": "MODE=test\n"},
    )

    head = _git(origin, "rev-parse", "skynet/optimize-1")
    assert _git(origin, "show", f"{head}:data/train.jsonl") == "{}"
    assert _git(origin, "show", f"{head}:.env") == "MODE=test"
    assert _git(origin, "show", f"{head}:app.py") == "x = 2"


def test_push_refuses_a_draft_in_a_read_only_path(tmp_path: Path) -> None:
    """A draft inside a submodule or over a Git LFS file is refused, and nothing is pushed."""
    origin = tmp_path / "origin"
    pinned = _repository(origin)

    with pytest.raises(RepoPublishError, match="read-only"):
        push_version(
            "acme/app",
            pinned,
            "",
            "skynet/optimize-1",
            "Apply it",
            None,
            remote=f"file://{origin}",
            drafts={"vendor/lib/x.py": "x"},
            readonly_paths=("vendor/lib",),
        )

    assert "skynet/optimize-1" not in _git(origin, "branch", "--list")


def test_push_refuses_a_version_that_does_not_apply(tmp_path: Path) -> None:
    """A stale patch fails in plain words and pushes nothing."""
    origin = tmp_path / "origin"
    pinned = _repository(origin)
    stale = "diff --git a/src/app.py b/src/app.py\n--- a/src/app.py\n+++ b/src/app.py\n@@ -1 +1 @@\n-x = 9\n+x = 2\n"

    with pytest.raises(RepoPublishError, match="no longer applies"):
        push_version("acme/app", pinned, stale, "skynet/optimize-1", "Apply it", None, remote=f"file://{origin}")

    assert "skynet/optimize-1" not in _git(origin, "branch", "--list")


def _github(
    monkeypatch: pytest.MonkeyPatch, refuse_draft: bool = False, open_pulls: list[dict[str, Any]] | None = None
) -> list[dict[str, Any]]:
    """Stand in for GitHub's repository and pull request endpoints.

    A pull request that the fake opens is listed as open from then on.

    Args:
        monkeypatch: Pytest fixture.
        refuse_draft: Answer a draft request the way a free private repository does.
        open_pulls: The open pull requests that the list endpoint returns; ``None`` for none.

    Returns:
        The pull request bodies sent, in order.
    """
    sent: list[dict[str, Any]] = []
    pulls = [] if open_pulls is None else open_pulls

    def fake_request(method: str, url: str, *, json: dict[str, Any], **_: Any) -> httpx.Response:
        """Record the pull request body and answer as GitHub does.

        Args:
            method: HTTP method.
            url: Request URL.
            json: Pull request body.
            **_: Other request options, ignored.

        Returns:
            The created pull request response.

        Raises:
            DomainError: A draft request when ``refuse_draft`` is set.
        """
        assert (method, url) == ("POST", "https://api.github.com/repos/acme/app/pulls")
        sent.append(json)
        if refuse_draft and json.get("draft"):
            raise DomainError("connectors.provider_error", status=502, provider="GitHub", status_code=422)
        body = {"html_url": "https://github.com/acme/app/pull/7", "number": 7, "draft": bool(json.get("draft"))}
        pulls.append({**body, "head": f"acme:{json['head']}", "base": json["base"]})
        return httpx.Response(201, json=body)

    def fake_get_json(url: str, *, params: dict[str, str] | None = None, **_: Any) -> Any:
        """Answer the repository and the open pull request list as GitHub does.

        Args:
            url: Request URL.
            params: Query parameters.
            **_: Other request options, ignored.

        Returns:
            The repository, or the open pull requests of the asked head, and of the asked base when one is given.
        """
        if url == "https://api.github.com/repos/acme/app/pulls":
            assert params is not None
            assert params["state"] == "open"
            return [
                pull
                for pull in pulls
                if pull["head"] == params["head"] and params.get("base", pull["base"]) == pull["base"]
            ]
        assert url == "https://api.github.com/repos/acme/app"
        return {"default_branch": "trunk"}

    monkeypatch.setattr(github_publish, "request", fake_request)
    monkeypatch.setattr(github_publish, "get_json", fake_get_json)
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


@pytest.mark.parametrize(("attacks", "fixed"), [(-1, 0), (0, -1), (True, 0), (0, False), (1.0, 0), ("3", 0)])
def test_hardening_summary_refuses_bad_counts(attacks: Any, fixed: Any) -> None:
    """Refuse negative, bool and non-int counts, so the body never reports a wrong number."""
    with pytest.raises(ValueError, match="whole numbers of zero or more"):
        HardeningSummary(attacks=attacks, exploits_fixed=fixed)


def test_hardening_summary_refuses_more_fixes_than_attacks() -> None:
    """Refuse a summary that fixed more exploits than it ran attacks."""
    with pytest.raises(ValueError, match="more exploits than"):
        HardeningSummary(attacks=2, exploits_fixed=3)


def test_hardening_summary_takes_zero_counts() -> None:
    """Accept a job that ran no attacks and fixed nothing."""
    assert github_publish.hardening_line(HardeningSummary(attacks=0, exploits_fixed=0)) == (
        "Metric survived 0 attacks, 0 exploits fixed"
    )


def test_pull_request_returns_the_open_one_for_the_same_head(monkeypatch: pytest.MonkeyPatch) -> None:
    """An open pull request for the head is returned, and no new one is opened."""
    existing = {
        "html_url": "https://github.com/acme/app/pull/3",
        "number": 3,
        "draft": True,
        "head": "acme:skynet/x",
        "base": "main",
    }
    other = {
        "html_url": "https://github.com/acme/app/pull/4",
        "number": 4,
        "draft": False,
        "head": "acme:skynet/y",
        "base": "main",
    }
    sent = _github(monkeypatch, open_pulls=[existing, other])

    change = open_pull_request("tok", "acme/app", head="skynet/x", base_branch="main", title="T", body="B")

    assert sent == []
    assert (change.branch, change.url, change.number, change.draft) == (
        "skynet/x",
        "https://github.com/acme/app/pull/3",
        3,
        True,
    )


def test_pull_request_for_the_same_head_into_another_base_is_not_reused(monkeypatch: pytest.MonkeyPatch) -> None:
    """An open pull request of the head into another base is not returned: a new one opens into the asked base."""
    elsewhere = {
        "html_url": "https://github.com/acme/app/pull/3",
        "number": 3,
        "draft": True,
        "head": "acme:skynet/x",
        "base": "release",
    }
    sent = _github(monkeypatch, open_pulls=[elsewhere])

    change = open_pull_request("tok", "acme/app", head="skynet/x", base_branch="main", title="T", body="B")

    assert [body["base"] for body in sent] == ["main"]
    assert change.number == 7


def test_pull_request_with_no_base_reuses_the_open_one_into_any_base(monkeypatch: pytest.MonkeyPatch) -> None:
    """With no base named, the lookup does not filter by base, so the open pull request of the head is returned."""
    elsewhere = {
        "html_url": "https://github.com/acme/app/pull/3",
        "number": 3,
        "draft": True,
        "head": "acme:skynet/x",
        "base": "release",
    }
    sent = _github(monkeypatch, open_pulls=[elsewhere])

    change = open_pull_request("tok", "acme/app", head="skynet/x", base_branch=None, title="T", body="B")

    assert sent == []
    assert change.number == 3


def test_pull_request_reports_a_refused_list(monkeypatch: pytest.MonkeyPatch) -> None:
    """When GitHub refuses the open pull request list, the call fails in plain words and opens nothing."""
    sent = _github(monkeypatch)

    def refuse(*_: Any, **__: Any) -> Any:
        """Fail as GitHub does on a server error.

        Raises:
            DomainError: Always.
        """
        raise DomainError("connectors.provider_error", status=502, provider="GitHub", status_code=500)

    monkeypatch.setattr(github_publish, "get_json", refuse)

    with pytest.raises(RepoPublishError, match="list the open pull requests"):
        open_pull_request("tok", "acme/app", head="skynet/x", base_branch="main", title="T", body="B")
    assert sent == []


def test_a_second_push_of_the_same_version_pushes_nothing(tmp_path: Path) -> None:
    """A retry finds the same tree on the branch, keeps its commit and returns it."""
    origin = tmp_path / "origin"
    pinned = _repository(origin)
    patch = _patch(origin, pinned)

    first = push_version("acme/app", pinned, patch, "skynet/optimize-1", "Apply it", None, remote=f"file://{origin}")
    again = push_version("acme/app", pinned, patch, "skynet/optimize-1", "Again", None, remote=f"file://{origin}")

    assert first == again == _git(origin, "rev-parse", "skynet/optimize-1")
    assert _git(origin, "log", "-1", "--format=%s", "skynet/optimize-1") == "Apply it"


def test_a_push_onto_a_branch_with_other_content_raises(tmp_path: Path) -> None:
    """A branch that holds another tree is not replaced: the push raises the typed error."""
    origin = tmp_path / "origin"
    pinned = _repository(origin)
    patch = _patch(origin, pinned)
    _git(origin, "branch", "skynet/optimize-1", "main")
    before = _git(origin, "rev-parse", "skynet/optimize-1")

    with pytest.raises(BranchConflictError, match="other content"):
        push_version("acme/app", pinned, patch, "skynet/optimize-1", "Apply it", None, remote=f"file://{origin}")

    assert _git(origin, "rev-parse", "skynet/optimize-1") == before


def test_a_branch_whose_name_ends_the_same_is_not_the_branch(tmp_path: Path) -> None:
    """A ref ``a/refs/heads/<branch>`` with other content is not the branch: the push still goes through."""
    origin = tmp_path / "origin"
    pinned = _repository(origin)
    patch = _patch(origin, pinned)
    _git(origin, "branch", "a/refs/heads/skynet/optimize-1", "main")

    pushed = push_version("acme/app", pinned, patch, "skynet/optimize-1", "Apply it", None, remote=f"file://{origin}")

    assert pushed == _git(origin, "rev-parse", "refs/heads/skynet/optimize-1")
    assert _git(origin, "rev-parse", f"{pushed}^") == pinned
    assert _git(origin, "rev-parse", "refs/heads/a/refs/heads/skynet/optimize-1") == _git(origin, "rev-parse", "main")


def test_candidates_list_the_remote_branches_once(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    """Many candidates make one ls-remote call; an existing branch is matched by its full name only."""
    origin = tmp_path / "origin"
    pinned = _repository(origin)
    patch = _patch(origin, pinned)
    kept = push_version("acme/app", pinned, patch, "skynet/candidate-v1", "Earlier", None, remote=f"file://{origin}")
    _git(origin, "branch", "a/refs/heads/skynet/candidate-v2", "main")
    git = github_publish._git
    listed: list[list[str]] = []

    def counting_git(arguments: list[str], *rest: Any) -> str:
        """Record each ls-remote call, then run git.

        Args:
            arguments: The git arguments.
            *rest: The working folder and the environment.

        Returns:
            The git output.
        """
        if arguments[0] == "ls-remote":
            listed.append(arguments)
        return git(arguments, *rest)

    monkeypatch.setattr(github_publish, "_git", counting_git)

    pushed = push_candidates(
        "acme/app", pinned, [("v1", patch), ("v2", ""), ("v3", patch)], "v2", None, remote=f"file://{origin}"
    )

    assert len(listed) == 1
    assert pushed["skynet/candidate-v1"] == kept
    assert pushed["skynet/candidate-v2"] == pinned == _git(origin, "rev-parse", "refs/heads/skynet/candidate-v2")
    assert _git(origin, "rev-parse", f"{pushed['skynet/candidate-v3']}^{{tree}}") == _git(
        origin, "rev-parse", f"{kept}^{{tree}}"
    )


def test_a_candidate_branch_with_other_content_raises(tmp_path: Path) -> None:
    """A candidate branch that holds another tree stops the push, and no branch moves."""
    origin = tmp_path / "origin"
    pinned = _repository(origin)
    patch = _patch(origin, pinned)
    _git(origin, "branch", "skynet/candidate-v1", "main")

    with pytest.raises(BranchConflictError, match="other content"):
        push_candidates("acme/app", pinned, [("v1", patch)], "v1", None, remote=f"file://{origin}")

    assert _git(origin, "rev-parse", "skynet/candidate-v1") == _git(origin, "rev-parse", "main")
    assert BEST_BRANCH not in _git(origin, "branch", "--list")


def test_a_candidate_branch_with_the_same_tree_keeps_its_commit(tmp_path: Path) -> None:
    """A candidate branch that holds the same tree under another commit is kept, and best points at it."""
    origin = tmp_path / "origin"
    pinned = _repository(origin)
    patch = _patch(origin, pinned)
    kept = push_version("acme/app", pinned, patch, "skynet/candidate-v1", "Earlier", None, remote=f"file://{origin}")

    pushed = push_candidates("acme/app", pinned, [("v1", patch)], "v1", None, remote=f"file://{origin}")

    assert pushed == {"skynet/candidate-v1": kept, BEST_BRANCH: kept}
    assert _git(origin, "rev-parse", "skynet/candidate-v1") == _git(origin, "rev-parse", BEST_BRANCH) == kept


@pytest.fixture
def effects(tmp_path: Path) -> Iterator[StepEffects]:
    """Yield the effect log bound to one durable step of job ``job-1``.

    Args:
        tmp_path: Pytest's temporary folder.

    Yields:
        The bound effect log; its step log closes after the test.
    """
    engine = create_engine(f"sqlite:///{tmp_path / 'db.sqlite'}")
    Base.metadata.create_all(engine)
    with Session(engine) as session:
        session.add(JobModel(optimization_id="job-1", username="ada", execution_generation=1))
        session.commit()
    steps = StepLogStore(engine, LocalObjectStore(tmp_path / "objects"), "w1", flush_window_seconds=60.0)
    step_id = steps.commit(
        run_id="job-1", agent_id="agent-a", seq=0, kind="model", execution_generation=1, grant_list_id="g", body={}
    )
    steps.flush()
    yield StepEffects(EffectStore(engine, steps), "job-1", step_id, 1)
    steps.close()


def _no_git(*_: Any, **__: Any) -> str:
    """Fail the test: a replayed step must not run git.

    Raises:
        AssertionError: Always.
    """
    raise AssertionError("a replayed step ran git")


def test_a_replayed_push_returns_the_stored_commit_without_git(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, effects: StepEffects
) -> None:
    """The second push of a step returns the stored commit and does not run git."""
    origin = tmp_path / "origin"
    pinned = _repository(origin)
    patch = _patch(origin, pinned)
    first = push_version(
        "acme/app", pinned, patch, "skynet/optimize-1", "Apply it", None, remote=f"file://{origin}", effects=effects
    )
    monkeypatch.setattr(github_publish, "_git", _no_git)

    again = push_version(
        "acme/app", pinned, patch, "skynet/optimize-1", "Apply it", None, remote=f"file://{origin}", effects=effects
    )

    assert again == first == _git(origin, "rev-parse", "skynet/optimize-1")


def test_replayed_candidates_return_the_stored_branches_without_git(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, effects: StepEffects
) -> None:
    """The second candidate push of a step returns the stored branches and does not run git."""
    origin = tmp_path / "origin"
    pinned = _repository(origin)
    patch = _patch(origin, pinned)
    first = push_candidates("acme/app", pinned, [("v1", patch)], "v1", None, remote=f"file://{origin}", effects=effects)
    monkeypatch.setattr(github_publish, "_git", _no_git)

    again = push_candidates("acme/app", pinned, [("v1", patch)], "v1", None, remote=f"file://{origin}", effects=effects)

    assert again == first
    assert first[BEST_BRANCH] == _git(origin, "rev-parse", BEST_BRANCH)


def _crash_first_commit(monkeypatch: pytest.MonkeyPatch) -> None:
    """Make the next database commit die, as a worker crash after the GitHub call does.

    Args:
        monkeypatch: Pytest fixture.
    """
    commit = Session.commit
    crashes = [1]

    def crash(session: Session) -> None:
        """Die in place of the first commit, then commit as usual.

        Args:
            session: The committing session.

        Raises:
            ConnectionError: On the first commit.
        """
        if crashes:
            crashes.pop()
            raise ConnectionError("worker died")
        commit(session)

    monkeypatch.setattr(Session, "commit", crash)


def test_a_crash_after_the_push_and_before_the_effect_commit_pushes_once(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, effects: StepEffects
) -> None:
    """The push landed, then the worker died: the retry finds the same tree and does not push again."""
    origin = tmp_path / "origin"
    pinned = _repository(origin)
    patch = _patch(origin, pinned)
    _crash_first_commit(monkeypatch)

    with pytest.raises(ConnectionError):
        push_version(
            "acme/app", pinned, patch, "skynet/optimize-1", "Apply it", None, remote=f"file://{origin}", effects=effects
        )
    landed = _git(origin, "rev-parse", "skynet/optimize-1")

    again = push_version(
        "acme/app", pinned, patch, "skynet/optimize-1", "Retry", None, remote=f"file://{origin}", effects=effects
    )

    assert again == landed == _git(origin, "rev-parse", "skynet/optimize-1")
    assert _git(origin, "log", "-1", "--format=%s", "skynet/optimize-1") == "Apply it"


def test_a_crash_after_the_pull_request_and_before_the_effect_commit_opens_one(
    monkeypatch: pytest.MonkeyPatch, effects: StepEffects
) -> None:
    """The pull request opened, then the worker died: the retry finds it open and opens no second one."""
    sent = _github(monkeypatch)
    _crash_first_commit(monkeypatch)

    with pytest.raises(ConnectionError):
        open_pull_request("tok", "acme/app", head="skynet/x", base_branch="main", title="T", body="B", effects=effects)
    change = open_pull_request(
        "tok", "acme/app", head="skynet/x", base_branch="main", title="T", body="B", effects=effects
    )
    replayed = open_pull_request(
        "tok", "acme/app", head="skynet/x", base_branch="main", title="T", body="B", effects=effects
    )

    assert len(sent) == 1
    assert change == replayed
    assert (change.branch, change.number, change.draft) == ("skynet/x", 7, True)


def test_one_step_pushes_two_branches_as_two_effects(tmp_path: Path, effects: StepEffects) -> None:
    """One step that pushes two branches pushes both; the second does not get the first branch's stored result."""
    origin = tmp_path / "origin"
    pinned = _repository(origin)
    patch = _patch(origin, pinned)

    one = push_version("acme/app", pinned, patch, "skynet/one", "One", None, remote=f"file://{origin}", effects=effects)
    two = push_version("acme/app", pinned, patch, "skynet/two", "Two", None, remote=f"file://{origin}", effects=effects)

    assert one == _git(origin, "rev-parse", "skynet/one")
    assert two == _git(origin, "rev-parse", "skynet/two")
    assert _git(origin, "log", "-1", "--format=%s", "skynet/two") == "Two"


def test_one_step_opens_pull_requests_for_two_heads(monkeypatch: pytest.MonkeyPatch, effects: StepEffects) -> None:
    """One step that opens pull requests for two heads opens both, and each replay returns its own."""
    sent = _github(monkeypatch)

    x = open_pull_request("tok", "acme/app", head="skynet/x", base_branch="main", title="T", body="B", effects=effects)
    y = open_pull_request("tok", "acme/app", head="skynet/y", base_branch="main", title="T", body="B", effects=effects)
    again = open_pull_request(
        "tok", "acme/app", head="skynet/x", base_branch="main", title="T", body="B", effects=effects
    )

    assert [body["head"] for body in sent] == ["skynet/x", "skynet/y"]
    assert (x.branch, y.branch, again) == ("skynet/x", "skynet/y", x)


def _no_github(*_: Any, **__: Any) -> Any:
    """Fail the test: a replayed step must not call GitHub.

    Raises:
        AssertionError: Always.
    """
    raise AssertionError("a replayed step called GitHub")


def test_a_replayed_pull_request_returns_the_stored_change_without_github(
    monkeypatch: pytest.MonkeyPatch, effects: StepEffects
) -> None:
    """The second pull request call of a step returns the stored change and makes no GitHub call."""
    _github(monkeypatch)
    first = open_pull_request(
        "tok", "acme/app", head="skynet/x", base_branch="main", title="T", body="B", effects=effects
    )
    monkeypatch.setattr(github_publish, "get_json", _no_github)
    monkeypatch.setattr(github_publish, "request", _no_github)

    again = open_pull_request(
        "tok", "acme/app", head="skynet/x", base_branch="main", title="T", body="B", effects=effects
    )

    assert (
        again
        == first
        == PublishedChange(branch="skynet/x", url="https://github.com/acme/app/pull/7", number=7, draft=True)
    )
